#!/usr/bin/env python3
"""Two-pane HTML dashboard for kesha-parrot's board activity.

Usage:  ./venv/bin/python dashboard.py [output.html]

Left: thread list. Right: per-thread stats, charts, votes, mentions, full stream.
Everything inlined — no network, no CDN, opens offline.
"""
import html
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import server  # noqa: E402

AGENT = "kesha-parrot"
STATE = Path(__file__).parent / "state.json"
KRSK = timezone(timedelta(hours=7))
ICON = ("data:image/svg+xml,"
        "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
        "%3Crect width='64' height='64' rx='14' fill='%230071e3'/%3E"
        "%3Cpath d='M40 16c-7 0-13 5-14 12l-6 3c-1 .5-1 2 0 2.5l5 2.5c.5 4 3 7 6.5 8.5L30 52h6l1.5-7h4l1.5 7h6l-2-9c4-2.5 6.5-7 6.5-12 0-8-6-15-13-15z' fill='%23fff'/%3E"
        "%3Ccircle cx='43' cy='25' r='2.6' fill='%230071e3'/%3E"
        "%3Cpath d='M20 31l-6-2.5c-2-1-2-3.5 0-4.5l6-2.5z' fill='%23ffd60a'/%3E"
        "%3C/svg%3E")


PALETTE = ["#0071e3", "#bf5af2", "#ff9f0a", "#30d158", "#ff375f", "#64d2ff",
           "#5e5ce6", "#ff6482", "#40c8e0", "#ac8e68", "#32ade6", "#ffd60a"]


def color(name):
    return PALETTE[sum(ord(c) for c in name or "") % len(PALETTE)]


def md(text):
    blocks = []
    text = re.sub(r"```[a-z]*\n(.*?)```",
                  lambda m: blocks.append(html.escape(m.group(1))) or f"\x00{len(blocks)-1}\x00",
                  text, flags=re.S)
    out, lst = [], False
    for line in html.escape(text).split("\n"):
        if re.match(r"^#{1,6} ", line):
            out.append(f"<h4>{line.lstrip('# ')}</h4>"); continue
        it = re.match(r"^[-*] (.+)", line) or re.match(r"^\d+\. (.+)", line)
        if it:
            if not lst: out.append("<ul>"); lst = True
            out.append(f"<li>{it.group(1)}</li>"); continue
        if lst: out.append("</ul>"); lst = False
        out.append(f"<p>{line}</p>" if line.strip() else "")
    if lst: out.append("</ul>")
    s = "\n".join(out)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\w)`([^`\n]+)`(?!\w)", r"<code>\1</code>", s)
    s = re.sub(r"(?<![\w/])@([a-z0-9-]{3,40})", r'<span class="m">@\1</span>', s)
    s = re.sub(r"(https?://[^\s<)]+)", r'<a href="\1" target="_blank">\1</a>', s)
    for i, b in enumerate(blocks):
        s = s.replace(f"\x00{i}\x00", f"<pre>{b}</pre>")
    return s


def collect():
    known = set(json.loads(STATE.read_text()).get("threads", {})) if STATE.exists() else set()
    before, scanned, mine_ids = 0, 0, {}
    for _ in range(20):
        d = server._call("GET", f"/v1/activity?limit=30{f'&before={before}' if before else ''}")
        if d.get("error"):
            print(f"  ! scan aborted: {d['error']}", file=sys.stderr)
            break
        items = d.get("items") or []
        if not items: break
        scanned += len(items)
        for it in items:
            if it.get("author") == AGENT:
                known.add(it.get("thread_id") or it["id"])
                mine_ids[it["id"]] = it.get("score", 0)
        before = d.get("next_before") or 0
        if not before: break

    threads = []
    for tid in known:
        d = server._call("GET", f"/v1/posts/{tid}?limit=30")
        post = d.get("post") or {}
        if not post: continue
        reps = (d.get("replies") or {}).get("items", [])
        nb, g = (d.get("replies") or {}).get("next_before"), 0
        while nb and g < 8:
            m = server._call("GET", f"/v1/posts/{tid}?limit=30&before={nb}")
            got = (m.get("replies") or {}).get("items", [])
            if not got: break
            reps += got; nb = (m.get("replies") or {}).get("next_before"); g += 1
        reps.sort(key=lambda r: r.get("seq", 0))
        # votes on my own items in this thread
        votes = []
        for it in [post] + reps:
            if it.get("author") == AGENT and it.get("score"):
                v = server._call("GET", f"/jovan?board=named&post_id={it['id']}&voters=true")
                for x in v.get("votes", []):
                    votes.append({"voter": x["voter"], "at": x["created_at"],
                                  "seq": it.get("seq"), "weight": x.get("weight", 1)})
        threads.append({"post": post, "replies": reps, "mine": post.get("author") == AGENT,
                        "votes": votes})
    threads.sort(key=lambda t: max([r.get("created_at", 0) for r in t["replies"]]
                                   + [t["post"].get("created_at", 0)]), reverse=True)
    STATE.write_text(json.dumps({"threads": {t["post"]["id"]: {} for t in threads},
                                 "updated": int(time.time())}, indent=1))
    outgoing = server._call("GET", "/jovan?voter=06df3f77-0755-44e9-9b1d-b2916eaeac0f").get("votes", [])
    me = server._call("GET", "/v1/me")
    return {"threads": threads, "scanned": scanned, "me": me, "outgoing": outgoing}


def build_payload(data):
    out = []
    for t in data["threads"]:
        msgs = []
        for i, it in enumerate([t["post"]] + t["replies"]):
            body = it.get("body") or it.get("preview") or ""
            msgs.append({
                "seq": it.get("seq"), "a": it.get("author", "?"), "t": it.get("created_at", 0),
                "len": len(body), "root": i == 0, "score": it.get("score", 0),
                "title": it.get("title") or "",
                "html": md(body),
                "mentions": sorted(set(re.findall(r"(?<![\w/])@([a-z0-9-]{3,40})", body))),
            })
        out.append({"id": t["post"]["id"], "topic": t["post"].get("topic", "—"),
                    "title": t["post"].get("title") or "(ответ в чужом треде)",
                    "mine": t["mine"], "votes": t["votes"], "msgs": msgs})
    return out


def render(data):
    payload = build_payload(data)
    me = data["me"]
    allmsg = [m for t in payload for m in t["msgs"]]
    mine = [m for m in allmsg if m["a"] == AGENT]
    peers = Counter(m["a"] for m in allmsg if m["a"] != AGENT)
    inbound = sum(len(t["votes"]) for t in payload)
    j = json.dumps(payload, ensure_ascii=False)
    out_votes = json.dumps(data["outgoing"], ensure_ascii=False)
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Доска агентов · kesha-parrot</title>\n<link rel="icon" href="{ICON}">\n<link rel="apple-touch-icon" href="{ICON}">\n<meta name="theme-color" content="#0071e3">
<style>
:root{{--ink:#1d1d1f;--dim:#86868b;--line:#e8e8ed;--bg:#f5f5f7;--card:#fff;--ac:#0071e3}}
*{{box-sizing:border-box;-webkit-font-smoothing:antialiased}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Segoe UI",sans-serif}}
.top{{background:var(--card);border-bottom:1px solid var(--line);padding:18px 26px;position:sticky;top:0;z-index:9}}
.top h1{{margin:0 0 3px;font-size:21px;letter-spacing:-.02em;font-weight:650}}
.top .s{{color:var(--dim);font-size:12.5px}}
.kpis{{display:flex;gap:9px;margin-top:14px;flex-wrap:wrap}}
.k{{background:var(--bg);border-radius:11px;padding:9px 14px;min-width:88px}}
.k b{{display:block;font-size:22px;letter-spacing:-.02em;line-height:1.15;font-weight:600}}
.k span{{font-size:11px;color:var(--dim)}}
.layout{{display:grid;grid-template-columns:330px 1fr;gap:0;height:calc(100vh - 132px)}}
.list{{overflow-y:auto;border-right:1px solid var(--line);background:var(--card)}}
.li{{padding:13px 18px;border-bottom:1px solid var(--line);cursor:pointer;transition:background .12s}}
.li:hover{{background:#fafafc}} .li.on{{background:#eef6ff;box-shadow:inset 3px 0 0 var(--ac)}}
.li .tp{{font-size:10.5px;font-weight:600;letter-spacing:.05em;text-transform:uppercase}}
.li .ti{{font-size:14px;font-weight:550;margin:4px 0 5px;line-height:1.35}}
.li .mt{{font-size:11.5px;color:var(--dim);display:flex;gap:9px;align-items:center}}
.dot{{width:6px;height:6px;border-radius:50%;display:inline-block}}
.pane{{overflow-y:auto;padding:24px 30px 60px}}
.card{{background:var(--card);border-radius:15px;padding:19px 21px;margin-bottom:15px}}
.card h3{{margin:0 0 15px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);font-weight:600}}
.charts{{display:grid;grid-template-columns:1fr 1fr;gap:15px}}
@media(max-width:1100px){{.charts{{grid-template-columns:1fr}}.layout{{grid-template-columns:1fr;height:auto}}.list{{max-height:300px}}}}
.bar{{display:flex;align-items:center;gap:9px;margin-bottom:7px;font-size:13px}}
.bar .n{{width:150px;text-align:right;color:#3a3a3c;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.bar .t{{flex:1;height:19px;border-radius:5px;position:relative;background:#f0f0f3}}
.bar .f{{height:100%;border-radius:5px}}
.bar .v{{width:44px;font-size:12px;color:var(--dim);font-variant-numeric:tabular-nums}}
.tl{{display:flex;align-items:flex-end;gap:2px;height:96px;padding-top:8px}}
.tl div{{flex:1;border-radius:3px 3px 0 0;min-height:2px;position:relative}}
.tlx{{display:flex;justify-content:space-between;color:var(--dim);font-size:10.5px;margin-top:6px}}
.msg{{display:flex;gap:12px;padding:17px 0;border-bottom:1px solid #f2f2f5}}
.msg.me{{background:linear-gradient(90deg,#f0f7ff,rgba(240,247,255,0));margin:0 -12px;padding:17px 12px;border-radius:11px;border-bottom:none}}
.av{{width:34px;height:34px;border-radius:50%;flex-shrink:0;display:grid;place-items:center;color:#fff;font-size:12px;font-weight:650}}
.mh{{display:flex;gap:9px;align-items:baseline;flex-wrap:wrap;margin-bottom:2px}}
.mh b{{font-size:14px}} .mh span{{color:#9a9aa0;font-size:12px}}
.pr{{font-size:14.5px;color:#3a3a3c}} .pr p{{margin:0 0 9px}} .pr p:empty{{display:none}}
.pr h4{{font-size:14px;margin:15px 0 7px}} .pr ul{{margin:0 0 10px;padding-left:19px}}
.pr code{{background:#f0f0f3;border-radius:4px;padding:1px 5px;font-size:13px;font-family:ui-monospace,Menlo,monospace}}
.pr pre{{background:#1d1d1f;color:#e8e8ed;border-radius:9px;padding:13px 15px;overflow-x:auto;font:12.5px/1.5 ui-monospace,Menlo,monospace;margin:0 0 11px}}
.pr a{{color:var(--ac);text-decoration:none}} .m{{color:var(--ac);font-weight:550}}
.tag{{display:inline-block;font-size:11px;padding:3px 9px;border-radius:100px;background:#f0f0f3;color:#3a3a3c;margin:0 5px 5px 0}}
.vote{{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid #f5f5f7;font-size:13.5px}}
.vote:last-child{{border:none}}
.empty{{color:var(--dim);font-size:13.5px;padding:6px 0}}
</style>
<div class="top">
  <h1><img src="{ICON}" width="26" height="26" style="vertical-align:-5px;margin-right:8px;border-radius:7px">Доска агентов · kesha-parrot</h1>
  <div class="s">{datetime.now(KRSK):%d.%m.%Y %H:%M} Krsk · просмотрено {data['scanned']} записей ленты · карма {me.get('karma',0)}</div>
  <div class="kpis">
    <div class="k"><b style="color:#0071e3">{sum(1 for t in payload if t['mine'])}</b><span>моих тредов</span></div>
    <div class="k"><b>{len(mine)}</b><span>моих сообщений</span></div>
    <div class="k"><b style="color:#30d158">{len(allmsg)-len(mine)}</b><span>чужих в них</span></div>
    <div class="k"><b>{len(peers)}</b><span>собеседников</span></div>
    <div class="k"><b style="color:#bf5af2">{inbound}</b><span>лайков мне</span></div>
    <div class="k"><b style="color:#ff9f0a">{len(data['outgoing'])}</b><span>лайков от меня</span></div>
    <div class="k"><b>{sum(m['len'] for m in mine)//1000}k</b><span>символов написал</span></div>
  </div>
</div>
<div class="layout"><div class="list" id="list"></div><div class="pane" id="pane"></div></div>
<script>
const D={j}, OUT={out_votes}, ME="{AGENT}";
const PAL=["#0071e3","#bf5af2","#ff9f0a","#30d158","#ff375f","#64d2ff","#5e5ce6","#ff6482","#40c8e0","#ac8e68","#32ade6","#ffd60a"];
const col=n=>PAL[[...(n||"")].reduce((a,c)=>a+c.charCodeAt(0),0)%PAL.length];
const dt=t=>new Date(t*1000).toLocaleString("ru",{{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit",timeZone:"Asia/Krasnoyarsk"}});
const hh=t=>new Date(t*1000).toLocaleString("ru",{{hour:"2-digit",timeZone:"Asia/Krasnoyarsk"}});

function bars(items,total){{return items.map(([n,v])=>
 `<div class="bar"><div class="n">${{n}}</div><div class="t"><div class="f" style="width:${{Math.max(2,100*v/total)}}%;background:${{col(n)}}"></div></div><div class="v">${{v}}</div></div>`).join("")||'<div class="empty">нет данных</div>';}}

function timeline(msgs){{
 const by={{}}; msgs.forEach(m=>{{const k=hh(m.t); by[k]=(by[k]||0)+1;}});
 const keys=Object.keys(by).sort(); const mx=Math.max(...Object.values(by),1);
 return `<div class="tl">${{keys.map(k=>`<div style="height:${{100*by[k]/mx}}%;background:${{col(k)}}" title="${{k}}:00 — ${{by[k]}} сообщ."></div>`).join("")}}</div>
 <div class="tlx"><span>${{keys[0]}}:00</span><span>${{keys[keys.length-1]}}:00 Krsk</span></div>`;}}

function render(i){{
 const t=D[i];
 document.querySelectorAll(".li").forEach((e,k)=>e.classList.toggle("on",k===i));
 const byA={{}},lenA={{}},men={{}};
 t.msgs.forEach(m=>{{byA[m.a]=(byA[m.a]||0)+1; lenA[m.a]=(lenA[m.a]||0)+m.len;
   m.mentions.forEach(x=>men[x]=(men[x]||0)+1);}});
 const S=(o)=>Object.entries(o).sort((a,b)=>b[1]-a[1]);
 const mineN=t.msgs.filter(m=>m.a===ME).length;
 document.getElementById("pane").innerHTML=`
 <div class="card"><h3>${{t.topic}} · ${{t.mine?"мой тред":"чужой тред"}}</h3>
   <div style="font-size:19px;font-weight:600;letter-spacing:-.015em;line-height:1.3">${{t.title}}</div>
   <div style="margin-top:11px;color:#86868b;font-size:13px">${{t.msgs.length}} сообщений · ${{Object.keys(byA).length}} участников · моих ${{mineN}} · ${{Math.round(t.msgs.reduce((a,m)=>a+m.len,0)/1000)}}k символов · с ${{dt(t.msgs[0].t)}}</div></div>
 <div class="charts">
  <div class="card"><h3>Кто сколько написал</h3>${{bars(S(byA),Math.max(...Object.values(byA)))}}</div>
  <div class="card"><h3>Объём, символов</h3>${{bars(S(lenA),Math.max(...Object.values(lenA)))}}</div>
  <div class="card"><h3>Когда писали (по часам Krsk)</h3>${{timeline(t.msgs)}}</div>
  <div class="card"><h3>Кого упоминают</h3>${{S(men).length?S(men).slice(0,10).map(([n,v])=>`<span class="tag" style="border-left:3px solid ${{col(n)}}">@${{n}} · ${{v}}</span>`).join(""):'<div class="empty">упоминаний нет</div>'}}</div>
 </div>
 <div class="card"><h3>Лайки моим сообщениям в этом треде</h3>
  ${{t.votes.length?t.votes.map(v=>`<div class="vote"><span><span class="dot" style="background:${{col(v.voter)}}"></span> <b>${{v.voter}}</b> → #${{v.seq}}</span><span style="color:#86868b">${{dt(v.at)}} · вес ${{v.weight}}</span></div>`).join(""):'<div class="empty">пока нет</div>'}}</div>
 <div class="card"><h3>Диалог</h3>${{t.msgs.map(m=>`
   <div class="msg ${{m.a===ME?"me":""}}">
     <div class="av" style="background:${{col(m.a)}}">${{m.a.slice(0,2).toUpperCase()}}</div>
     <div style="min-width:0;flex:1">
       <div class="mh"><b style="color:${{col(m.a)}}">${{m.a}}</b><span>#${{m.seq}} · ${{dt(m.t)}} · ${{m.len}} симв.</span>${{m.score?`<span style="color:#c78a00">▲${{m.score}}</span>`:""}}</div>
       ${{m.root&&m.title?`<div style="font-size:17px;font-weight:600;margin:6px 0 9px;line-height:1.3">${{m.title}}</div>`:""}}
       <div class="pr">${{m.html}}</div></div></div>`).join("")}}</div>`;
 document.getElementById("pane").scrollTop=0;
}}
document.getElementById("list").innerHTML=D.map((t,i)=>{{
 const last=Math.max(...t.msgs.map(m=>m.t));
 return `<div class="li" onclick="render(${{i}})">
  <div class="tp" style="color:${{col(t.topic)}}">${{t.topic}}${{t.mine?"":" · чужой"}}</div>
  <div class="ti">${{t.title}}</div>
  <div class="mt"><span>${{t.msgs.length}} сообщ.</span><span>${{dt(last)}}</span>${{t.votes.length?`<span style="color:#bf5af2">▲${{t.votes.length}}</span>`:""}}</div></div>`;}}).join("");
render(0);
</script></html>"""


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/kesha/gpb-dashboard.html")
    d = collect()
    out.write_text(render(d), encoding="utf-8")
    print(f"{out} · тредов {len(d['threads'])} · {out.stat().st_size // 1024} КБ")

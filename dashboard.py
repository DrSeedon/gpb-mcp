#!/usr/bin/env python3
"""Two-pane HTML dashboard for one agent's board activity.

Whose activity is read from config.json — clone the repo, change two fields, and the
dashboard is about you. Nothing about this operator is baked into the code.

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
import stats as boardstats  # noqa: E402

HERE = Path(__file__).parent
CFG = json.loads((HERE / "config.json").read_text()) if (HERE / "config.json").exists() else {}
AGENT = CFG.get("agent", "")
AGENT_ID = CFG.get("agent_id", "")
STATE = HERE / "state.json"
KRSK = timezone(timedelta(hours=CFG.get("tz_offset_hours", 0)))
TZNAME = CFG.get("tz_name", "UTC")
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


CACHE = Path(__file__).parent / "cache.json"


def tip_unchanged() -> bool:
    """One request answers 'did anything happen anywhere on the board'.

    tip seq is a global monotonic counter: if it has not moved, nothing was posted by
    anyone, so polling N threads would cost N requests to learn what this one already
    knew. An error is NOT treated as 'unchanged' — that would be the silent-failure
    shape this codebase has been bitten by four times.
    """
    d = server._call("GET", "/v1/activity?limit=1")
    if d.get("error"):
        return False
    items = d.get("items") or []
    tip = items[0].get("seq", 0) if items else 0
    if not CACHE.exists():
        return False
    prev = json.loads(CACHE.read_text()).get("tip", 0)
    if tip and tip == prev:
        return True
    return False


def collect():
    """Incremental: keep every message ever seen in cache.json, fetch only what is new.

    A full rebuild re-downloads ~1200 items and takes minutes; a delta run costs one
    request per known thread plus one feed page. The cache is the source of truth for
    display, the API only ever adds to it.
    """
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {"threads": {}}
    threads = cache.get("threads", {})
    # seed from the legacy state file once: threads that have already scrolled past the
    # feed horizon can never be rediscovered by scanning, only remembered
    if STATE.exists():
        for tid in json.loads(STATE.read_text()).get("threads", {}):
            threads.setdefault(tid, {"msgs": [], "votes": [], "high": 0})

    # 1. discover threads I am in, from the feed head only (cheap)
    before, scanned = 0, 0
    for _ in range(6):
        d = server._call("GET", f"/v1/activity?limit=30{f'&before={before}' if before else ''}")
        if d.get("error"):
            print(f"  ! feed scan aborted: {d['error']}", file=sys.stderr)
            break
        items = d.get("items") or []
        if not items:
            break
        scanned += len(items)
        for it in items:
            if it.get("author") == AGENT:
                threads.setdefault(it.get("thread_id") or it["id"],
                                   {"msgs": [], "votes": [], "high": 0})
        before = d.get("next_before") or 0
        if not before:
            break

    # 2. per thread: pull only replies newer than the high-water mark we already hold
    for tid, box in threads.items():
        high = box.get("high", 0)
        q = f"/v1/posts/{tid}?limit=30" + (f"&after={high}" if high else "")
        d = server._call("GET", q)
        if d.get("error"):
            print(f"  ! thread {tid[:8]}: {d['error']}", file=sys.stderr)
            continue
        post = d.get("post") or {}
        if not post:
            continue
        box["post"] = {k: post.get(k) for k in
                       ("id", "seq", "author", "topic", "title", "created_at", "score")}
        box["post"]["body"] = post.get("body", "")
        fresh = (d.get("replies") or {}).get("items", [])
        nb, g = (d.get("replies") or {}).get("next_before"), 0
        while nb and g < 8:
            m = server._call("GET", f"/v1/posts/{tid}?limit=30&before={nb}")
            if m.get("error"):
                break
            got = (m.get("replies") or {}).get("items", [])
            if not got:
                break
            fresh += got
            nb, g = (m.get("replies") or {}).get("next_before"), g + 1
        seen = {m["seq"] for m in box["msgs"]}
        for r in fresh:
            if r.get("seq") not in seen:
                box["msgs"].append({k: r.get(k) for k in
                                    ("id", "seq", "author", "created_at", "score")}
                                   | {"body": r.get("body") or r.get("preview") or ""})
        box["msgs"].sort(key=lambda m: m.get("seq", 0))
        if box["msgs"]:
            box["high"] = max(box["high"], max(m["seq"] for m in box["msgs"]))

        # votes on my own items — only re-check items that show a score
        for it in [post] + fresh:
            if it.get("author") == AGENT and it.get("score"):
                v = server._call("GET", f"/jovan?board=named&post_id={it['id']}&voters=true")
                known = {(x["voter"], x["seq"]) for x in box["votes"]}
                for x in v.get("votes", []):
                    if (x["voter"], it.get("seq")) not in known:
                        box["votes"].append({"voter": x["voter"], "at": x["created_at"],
                                             "seq": it.get("seq"), "weight": x.get("weight", 1)})

    tipres = server._call("GET", "/v1/activity?limit=1")
    tipitems = tipres.get("items") or []
    cache["tip"] = tipitems[0].get("seq", 0) if tipitems else cache.get("tip", 0)
    cache["threads"] = threads
    cache["updated"] = int(time.time())
    CACHE.write_text(json.dumps(cache, ensure_ascii=False))

    out = []
    for tid, box in threads.items():
        if not box.get("post"):
            continue
        out.append({"post": box["post"], "replies": box["msgs"],
                    "mine": box["post"].get("author") == AGENT, "votes": box["votes"]})
    out.sort(key=lambda t: max([r.get("created_at", 0) for r in t["replies"]]
                               + [t["post"].get("created_at", 0)]), reverse=True)
    return {"threads": out, "scanned": scanned,
            "me": server._call("GET", "/v1/me"),
            "outgoing": (server._call("GET", f"/jovan?voter={AGENT_ID}").get("votes", [])
                         if AGENT_ID else []),
            "cached_msgs": sum(len(b["msgs"]) for b in threads.values())}


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
    try:
        stats_html = boardstats.render_stats()
    except Exception as e:  # a broken stat must not take the dashboard down with it
        stats_html = f'<div class="card"><div class="empty">статистика не собралась: {html.escape(str(e))}</div></div>'
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Доска агентов · {AGENT}</title>\n<link rel="icon" href="{ICON}">\n<link rel="apple-touch-icon" href="{ICON}">\n<meta name="theme-color" content="#0071e3">
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
/* ── tabs ── */
.tabs{{display:flex;gap:4px;margin-top:15px}}
.tb{{padding:7px 15px;border-radius:9px;font-size:13.5px;font-weight:550;cursor:pointer;color:var(--dim);transition:all .15s;user-select:none}}
.tb:hover{{background:var(--bg)}}
.tb.on{{background:var(--ink);color:#fff}}
#tab-stats{{display:none;padding:24px 26px 70px;max-width:1500px}}
#tab-stats.on{{display:block}} .layout.off{{display:none}}
.kpis.stat{{margin:0 0 16px}}
.kpis.stat .k{{background:var(--card);min-width:112px;padding:12px 16px}}
/* ── stat cards ── */
#tab-stats .charts{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:15px;align-items:start}}
#tab-stats .card.wide{{grid-column:1/-1}}
@media(max-width:1250px){{#tab-stats .charts{{grid-template-columns:1fr 1fr}}}}
@media(max-width:820px){{#tab-stats .charts{{grid-template-columns:1fr}}}}
.note{{color:var(--dim);font-size:11.8px;line-height:1.5;margin-top:12px;border-top:1px solid var(--line);padding-top:10px}}
.note b{{color:#3a3a3c}}
.note code{{background:#f0f0f3;border-radius:4px;padding:1px 5px;font-family:ui-monospace,Menlo,monospace}}
/* vertical histogram */
.vbs{{display:flex;align-items:flex-end;gap:6px}}
.vb{{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;height:100%}}
.vv{{font-size:11px;color:#3a3a3c;font-variant-numeric:tabular-nums;margin-bottom:4px}}
.vt{{width:100%;background:linear-gradient(180deg,#0071e3,#64d2ff);border-radius:5px 5px 2px 2px;min-height:2px}}
.vl{{font-size:10.5px;color:var(--dim);margin-top:6px;white-space:nowrap}}
/* heatmap */
.hm{{display:grid;gap:2px;font-size:10px}}
.hl{{text-align:center;color:var(--dim);font-variant-numeric:tabular-nums;padding-bottom:3px}}
.hr{{color:#3a3a3c;font-size:10.5px;text-align:right;padding-right:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;line-height:19px}}
.hc{{height:19px;border-radius:3px;display:grid;place-items:center;font-variant-numeric:tabular-nums}}
/* table */
.tbl{{width:100%;border-collapse:collapse;font-size:13px}}
.tbl th{{text-align:left;color:var(--dim);font-weight:550;font-size:11px;text-transform:uppercase;letter-spacing:.04em;padding:0 8px 8px 0;border-bottom:1px solid var(--line)}}
.tbl td{{padding:7px 8px 7px 0;border-bottom:1px solid #f5f5f7;vertical-align:top}}
.tbl tr:last-child td{{border:none}}
.tbl .num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
/* interactive line charts */
.chart{{position:relative;width:100%}}
.chart svg{{display:block;overflow:visible}}
.chart .tip,.dist .tip{{position:absolute;pointer-events:none;background:rgba(29,29,31,.94);color:#fff;
 border-radius:9px;padding:8px 11px;font-size:12px;line-height:1.45;white-space:nowrap;
 opacity:0;transition:opacity .1s;z-index:5;box-shadow:0 6px 22px rgba(0,0,0,.22)}}
.chart .tip b,.dist .tip b{{font-size:12.5px}}
.chart .tip i,.dist .tip i{{font-style:normal;color:#a1a1a6}}
.chart .tip .sw,.dist .tip .sw{{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px}}
.lgd{{display:flex;gap:14px;font-size:11.5px;color:var(--dim);margin-top:8px}}
.lgd i{{font-style:normal;display:inline-flex;align-items:center;gap:5px}}
.lgd .sw{{width:9px;height:3px;border-radius:2px;display:inline-block}}
/* chart left, ranked list right */
.split{{display:grid;grid-template-columns:1.6fr 1fr;gap:22px;align-items:start}}
.split .side h4{{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;
 color:var(--dim);font-weight:600}}
.split .side{{border-left:1px solid var(--line);padding-left:20px;max-height:430px;overflow-y:auto}}
.split .side .bar .n{{width:120px}}
@media(max-width:1000px){{.split{{grid-template-columns:1fr}}
 .split .side{{border-left:0;padding-left:0;border-top:1px solid var(--line);padding-top:16px}}}}
/* distribution widget */
.dist{{position:relative;width:100%}}
.dctl{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:10px;align-items:center}}
.seg{{display:inline-flex;background:#f0f0f3;border-radius:8px;padding:2px}}
.seg button{{border:0;background:none;font:inherit;font-size:11.5px;padding:4px 10px;border-radius:6px;
 cursor:pointer;color:var(--dim);transition:all .12s}}
.seg button.on{{background:#fff;color:var(--ink);font-weight:600;box-shadow:0 1px 3px rgba(0,0,0,.10)}}
.seg .lab{{font-size:10.5px;color:var(--dim);padding:5px 4px 5px 8px;text-transform:uppercase;letter-spacing:.04em}}
.dstat{{display:flex;gap:16px;font-size:11.5px;color:var(--dim);margin-top:9px;flex-wrap:wrap}}
.dstat b{{color:var(--ink);font-variant-numeric:tabular-nums}}
.dstat i{{font-style:normal;display:inline-flex;align-items:center;gap:5px}}
.dstat .sw{{width:10px;height:2.5px;display:inline-block;border-radius:2px}}
</style>
<div class="top">
  <h1><img src="{ICON}" width="26" height="26" style="vertical-align:-5px;margin-right:8px;border-radius:7px">Доска агентов · {AGENT}</h1>
  <div class="s">обновлено <b id="upd">{datetime.now(KRSK):%d.%m.%Y %H:%M:%S}</b> {TZNAME}<span id="ago"></span> ·
    {data['cached_msgs']} сообщений в кеше · просмотрено {data['scanned']} записей ленты · карма {me.get('karma',0)}</div>
  <div class="kpis">
    <div class="k"><b style="color:#0071e3">{sum(1 for t in payload if t['mine'])}</b><span>моих тредов</span></div>
    <div class="k"><b>{len(mine)}</b><span>моих сообщений</span></div>
    <div class="k"><b style="color:#30d158">{len(allmsg)-len(mine)}</b><span>чужих в них</span></div>
    <div class="k"><b>{len(peers)}</b><span>собеседников</span></div>
    <div class="k"><b style="color:#bf5af2">{inbound}</b><span>лайков мне</span></div>
    <div class="k"><b style="color:#ff9f0a">{len(data['outgoing'])}</b><span>лайков от меня</span></div>
    <div class="k"><b>{sum(m['len'] for m in mine)//1000}k</b><span>символов написал</span></div>
  </div>
  <div class="tabs">
    <div class="tb on" id="tbA" onclick="tab(0)">Мои треды</div>
    <div class="tb" id="tbB" onclick="tab(1)">Статистика доски</div>
  </div>
</div>
<div class="layout" id="tab-threads"><div class="list" id="list"></div><div class="pane" id="pane"></div></div>
<div id="tab-stats">{stats_html}</div>
<script>
function tab(i){{
 document.getElementById("tab-threads").classList.toggle("off",i===1);
 document.getElementById("tab-stats").classList.toggle("on",i===1);
 document.getElementById("tbA").classList.toggle("on",i===0);
 document.getElementById("tbB").classList.toggle("on",i===1);
 if(i===1)window.scrollTo(0,0);
 location.hash=i===1?"stats":"";
 // charts in a display:none pane have clientWidth 0 and silently draw nothing
 if(i===1&&typeof drawAll==="function")drawAll();
}}
const BUILT={int(time.time())};
setInterval(()=>{{const s=Math.floor(Date.now()/1000-BUILT);
 document.getElementById("ago").textContent=" · "+(s<90?s+" сек назад":Math.floor(s/60)+" мин назад");}},1000);
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

/* ── line charts: drawn client-side so the Y axis and the crosshair sit on real pixels ── */
const NS="http://www.w3.org/2000/svg";
function el(t,a){{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);return e;}}
function nice(m){{ // round the axis top to something a human reads
 if(m<=5)return 5; const p=Math.pow(10,Math.floor(Math.log10(m)));
 for(const s of [1,1.5,2,2.5,3,4,5,7.5,10]) if(m<=s*p) return s*p; return 10*p;}}
function fmt(v){{return v>=10000?(v/1000).toFixed(v%1000?1:0)+"k":String(Math.round(v*100)/100);}}

function drawChart(box){{
 const c=JSON.parse(box.dataset.c), W=box.clientWidth, H=c.h;
 if(!W)return;
 const L=46,R=12,T=12,B=22, iw=W-L-R, ih=H-T-B, n=c.labels.length;
 let mx=0; c.series.forEach(s=>s.values.forEach(v=>{{if(v>mx)mx=v;}}));
 const top=nice(mx)||1, X=i=>L+(n<2?0:iw*i/(n-1)), Y=v=>T+ih-ih*v/top;
 box.innerHTML="";
 const s=el("svg",{{width:W,height:H,viewBox:`0 0 ${{W}} ${{H}}`}});
 for(let g=0;g<=4;g++){{const v=top*g/4,y=Y(v);
  s.appendChild(el("line",{{x1:L,y1:y,x2:W-R,y2:y,stroke:g?"#eee":"#ddd","stroke-width":1}}));
  const t=el("text",{{x:L-8,y:y+3.5,"font-size":10,fill:"#9a9aa0","text-anchor":"end"}});
  t.textContent=fmt(v); s.appendChild(t);}}
 const every=Math.max(1,Math.ceil(n/Math.max(3,Math.floor(W/135))));
 let lastX=-1e9;
 c.labels.forEach((lb,i)=>{{ if(i%every&&i!==n-1)return;
  // the forced last label used to land on top of the previous one
  if(X(i)-lastX<58&&i===n-1)return;
  lastX=X(i);
  const t=el("text",{{x:X(i),y:H-5,"font-size":10,fill:"#9a9aa0",
   "text-anchor":i===0?"start":i>=n-2?"end":"middle"}});
  t.textContent=lb; s.appendChild(t);}});
 c.series.forEach(se=>{{
  const pts=se.values.map((v,i)=>`${{X(i)}},${{Y(v)}}`).join(" ");
  s.appendChild(el("polygon",{{points:`${{L}},${{Y(0)}} ${{pts}} ${{X(n-1)}},${{Y(0)}}`,
   fill:se.color,"fill-opacity":".12"}}));
  s.appendChild(el("polyline",{{points:pts,fill:"none",stroke:se.color,"stroke-width":2,
   "stroke-linejoin":"round","stroke-linecap":"round"}}));}});
 const rule=el("line",{{x1:0,y1:T,x2:0,y2:T+ih,stroke:"#1d1d1f","stroke-width":1,
  "stroke-dasharray":"3 3",opacity:0}}); s.appendChild(rule);
 const dots=c.series.map(se=>{{const d=el("circle",{{r:4.5,fill:"#fff",stroke:se.color,
  "stroke-width":2.5,opacity:0}}); s.appendChild(d); return d;}});
 box.appendChild(s);
 const tip=document.createElement("div"); tip.className="tip"; box.appendChild(tip);
 s.addEventListener("mousemove",ev=>{{
  const r=box.getBoundingClientRect();
  let i=Math.round((ev.clientX-r.left-L)/(iw/Math.max(1,n-1)));
  i=Math.max(0,Math.min(n-1,i));
  rule.setAttribute("x1",X(i)); rule.setAttribute("x2",X(i)); rule.setAttribute("opacity",.35);
  c.series.forEach((se,k)=>{{dots[k].setAttribute("cx",X(i));dots[k].setAttribute("cy",Y(se.values[i]));
   dots[k].setAttribute("opacity",1);}});
  const lbl=c.bucket?`${{c.labels[i]}} – ${{c.labels[i+1]||"конец"}} <i>(${{c.bucket}})</i>`:c.labels[i];
  tip.innerHTML=`<b>${{lbl}}</b>`+c.series.map(se=>
   `<div><span class="sw" style="background:${{se.color}}"></span>${{se.name}} <i>${{se.values[i]}}${{c.unit||""}}</i></div>`).join("");
  tip.style.opacity=1;
  const tw=tip.offsetWidth||140;
  tip.style.left=Math.max(2,Math.min(W-tw-2,X(i)-tw/2))+"px";
  tip.style.top="2px";
 }});
 s.addEventListener("mouseleave",()=>{{tip.style.opacity=0;rule.setAttribute("opacity",0);
  dots.forEach(d=>d.setAttribute("opacity",0));}});
}}
/* ── distributions: binned in the browser so the axes can switch lin/log ── */
function fmtV(v,kind){{
 if(kind==="sec"){{ if(v<90)return Math.round(v)+"с"; if(v<5400)return Math.round(v/60)+"м";
  if(v<172800)return (v/3600).toFixed(v<36000?1:0)+"ч"; return (v/86400).toFixed(1)+"д";}}
 if(v>=1000)return (v/1000).toFixed(v%1000&&v<10000?1:0)+"k";
 return String(Math.round(v*10)/10);}}

function bins(vals,xlog,target){{
 const mn=Math.min(...vals), mx=Math.max(...vals);
 if(xlog){{
  const lo=Math.max(mn,0.5), steps=[], per=6;             // 6 bins per decade
  const a=Math.floor(Math.log10(lo)*per), b=Math.ceil(Math.log10(mx)*per);
  for(let k=a;k<=b;k++)steps.push(Math.pow(10,k/per));
  const out=steps.slice(0,-1).map((s,i)=>({{lo:s,hi:steps[i+1],n:0}}));
  vals.forEach(v=>{{const t=Math.max(v,lo);
   let i=Math.floor(Math.log10(t)*per)-a; i=Math.max(0,Math.min(out.length-1,i)); out[i].n++;}});
  return out;
 }}
 // linear: a regular step across the whole range, empty steps kept as zeros
 const raw=(mx-mn)/target||1, p=Math.pow(10,Math.floor(Math.log10(raw)));
 let step=[1,2,2.5,5,10].map(x=>x*p).find(x=>x>=raw)||10*p;
 if(mx-mn<=40&&Number.isInteger(mn)&&Number.isInteger(mx))step=Math.max(1,Math.round(step));
 const start=Math.floor(mn/step)*step, out=[];
 for(let x=start;x<mx+step*0.5;x+=step)out.push({{lo:x,hi:x+step,n:0}});
 vals.forEach(v=>{{let i=Math.floor((v-start)/step); i=Math.max(0,Math.min(out.length-1,i)); out[i].n++;}});
 return out;
}}

function drawDist(box){{
 const d=JSON.parse(box.dataset.d);
 if(!box.dataset.init){{
  box.dataset.init="1"; box.dataset.xlog=d.xlog?"1":"0"; box.dataset.ymode="n"; box.dataset.ylog="0";
 }}
 const W=box.clientWidth; if(!W)return;
 const vals=d.v, n=vals.length, srt=[...vals].sort((a,b)=>a-b);
 const med=srt[Math.floor(n/2)], avg=vals.reduce((a,b)=>a+b,0)/n;
 const xlog=box.dataset.xlog==="1", pct=box.dataset.ymode==="p", ylog=box.dataset.ylog==="1";
 const bs=bins(vals,xlog,Math.max(14,Math.min(46,Math.floor(W/28))));
 const H=d.h, L=52, R=14, T=14, B=26, iw=W-L-R, ih=H-T-B;
 const vmax=Math.max(...bs.map(b=>b.n))||1;
 const ytop=pct?100*vmax/n:vmax;
 const yv=b=>pct?100*b.n/n:b.n;
 const Y=v=>{{ if(!ylog) return T+ih-ih*v/(nice(ytop)||1);
   const lo=pct?100*0.5/n:0.5, t=Math.log10(Math.max(v,lo)/lo)/Math.log10(nice(ytop)/lo);
   return v<=0?T+ih:T+ih-ih*Math.max(0,t);}};
 const xpos=v=>{{ if(!xlog) return L+iw*(v-bs[0].lo)/(bs[bs.length-1].hi-bs[0].lo);
   const a=Math.log10(Math.max(bs[0].lo,0.5)), b=Math.log10(bs[bs.length-1].hi);
   return L+iw*(Math.log10(Math.max(v,Math.pow(10,a)))-a)/(b-a);}};

 const ctl=`<div class="dctl">
   <span class="seg"><span class="lab">X</span>
     <button data-k="xlog" data-v="0" class="${{xlog?"":"on"}}">линейная</button>
     <button data-k="xlog" data-v="1" class="${{xlog?"on":""}}">лог</button></span>
   <span class="seg"><span class="lab">Y</span>
     <button data-k="ymode" data-v="n" class="${{pct?"":"on"}}">количество</button>
     <button data-k="ymode" data-v="p" class="${{pct?"on":""}}">проценты</button></span>
   <span class="seg"><span class="lab">шкала Y</span>
     <button data-k="ylog" data-v="0" class="${{ylog?"":"on"}}">линейная</button>
     <button data-k="ylog" data-v="1" class="${{ylog?"on":""}}">лог</button></span></div>`;

 let sv=`<svg width="${{W}}" height="${{H}}" viewBox="0 0 ${{W}} ${{H}}" style="display:block;overflow:visible">`;
 const top=nice(ytop)||1;
 for(let g=0;g<=4;g++){{
  const v=ylog?top*Math.pow(10,-(4-g)/1.6):top*g/4, y=Y(v);
  sv+=`<line x1="${{L}}" y1="${{y}}" x2="${{W-R}}" y2="${{y}}" stroke="${{g?"#eee":"#ddd"}}" stroke-width="1"/>`;
  sv+=`<text x="${{L-8}}" y="${{y+3.5}}" font-size="10" fill="#9a9aa0" text-anchor="end">${{
    pct?(v<1?v.toFixed(1):Math.round(v))+"%":fmtV(v,"num")}}</text>`;}}
 bs.forEach(b=>{{
  const x0=xpos(b.lo), x1=xpos(b.hi), w=Math.max(1.2,x1-x0-1.4), y=Y(yv(b));
  if(b.n>0)sv+=`<rect x="${{x0+0.7}}" y="${{y}}" width="${{w}}" height="${{Math.max(1,T+ih-y)}}" `
    +`rx="2.5" fill="url(#dg)"/>`;
 }});
 // median / mean / our own value — the three questions people actually ask of a histogram
 const marks=[["медиана",med,"#ff375f"],["среднее",avg,"#ff9f0a"]];
 if(d.mine!=null)marks.push(["мы",d.mine,"#30d158"]);
 const placed=[];
 marks.forEach(([lb,v,c])=>{{ const x=xpos(v); if(x<L-1||x>W-R+1)return;
  let row=0; while(placed.some(q=>q.row===row&&Math.abs(q.x-x)<92))row++;   // stagger, don't overlap
  placed.push({{x,row}});
  const ty=T+11+row*13, anchor=x>W-110?"end":"start", dx=x>W-110?-4:4;
  sv+=`<line x1="${{x}}" y1="${{T}}" x2="${{x}}" y2="${{T+ih}}" stroke="${{c}}" stroke-width="1.6" stroke-dasharray="4 3"/>`
    +`<text x="${{x+dx}}" y="${{ty}}" font-size="10" font-weight="600" fill="${{c}}" text-anchor="${{anchor}}">${{lb}} ${{fmtV(v,d.fmt)}}</text>`;}});
 const nt=Math.max(3,Math.min(9,Math.floor(W/110)));
 for(let i=0;i<=nt;i++){{
  const b0=bs[0].lo, b1=bs[bs.length-1].hi;
  const v=xlog?Math.pow(10,Math.log10(Math.max(b0,0.5))+(Math.log10(b1)-Math.log10(Math.max(b0,0.5)))*i/nt)
              :b0+(b1-b0)*i/nt;
  sv+=`<text x="${{xpos(v)}}" y="${{H-6}}" font-size="10" fill="#9a9aa0" text-anchor="${{
    i===0?"start":i===nt?"end":"middle"}}">${{fmtV(v,d.fmt)}}</text>`;}}
 sv+=`<defs><linearGradient id="dg" x1="0" y1="0" x2="0" y2="1">
   <stop offset="0" stop-color="#0071e3"/><stop offset="1" stop-color="#64d2ff"/></linearGradient></defs></svg>`;

 const q=p=>srt[Math.min(n-1,Math.floor(n*p))];
 const stat=`<div class="dstat">
  <i><span class="sw" style="background:#ff375f"></span>медиана <b>${{fmtV(med,d.fmt)}}</b></i>
  <i><span class="sw" style="background:#ff9f0a"></span>среднее <b>${{fmtV(avg,d.fmt)}}</b></i>
  ${{d.mine!=null?`<i><span class="sw" style="background:#30d158"></span>у нас <b>${{fmtV(d.mine,d.fmt)}}</b></i>`:""}}
  <i>p90 <b>${{fmtV(q(.9),d.fmt)}}</b></i><i>p99 <b>${{fmtV(q(.99),d.fmt)}}</b></i>
  <i>макс <b>${{fmtV(srt[n-1],d.fmt)}}</b></i><i>n=<b>${{n}}</b></i></div>`;

 box.innerHTML=ctl+sv+stat;
 const tip=document.createElement("div"); tip.className="tip"; box.appendChild(tip);
 const svgEl=box.querySelector("svg");
 svgEl.addEventListener("mousemove",ev=>{{
  const r=box.getBoundingClientRect(), x=ev.clientX-r.left;
  let best=null,bd=1e9;
  bs.forEach(b=>{{const c=(xpos(b.lo)+xpos(b.hi))/2; if(Math.abs(c-x)<bd){{bd=Math.abs(c-x);best=b;}}}});
  if(!best)return;
  let cum=0; for(const b of bs){{cum+=b.n; if(b===best)break;}}
  tip.innerHTML=`<b>${{fmtV(best.lo,d.fmt)}} – ${{fmtV(best.hi,d.fmt)}}</b>`
   +`<div>${{d.name}}: <i>${{best.n}}</i></div>`
   +`<div>доля: <i>${{(100*best.n/n).toFixed(1)}}%</i></div>`
   +`<div>накоплено: <i>${{(100*cum/n).toFixed(1)}}%</i></div>`;
  tip.style.opacity=1;
  const tw=tip.offsetWidth||150, cx=(xpos(best.lo)+xpos(best.hi))/2;
  tip.style.left=Math.max(2,Math.min(W-tw-2,cx-tw/2))+"px"; tip.style.top="34px";
 }});
 svgEl.addEventListener("mouseleave",()=>tip.style.opacity=0);
 box.querySelectorAll(".dctl button").forEach(b=>b.onclick=()=>{{
  box.dataset[b.dataset.k]=b.dataset.v; drawDist(box);}});
}}
function drawAll(){{document.querySelectorAll(".chart").forEach(drawChart);
 document.querySelectorAll(".dist").forEach(drawDist);}}
drawAll();
let rt; addEventListener("resize",()=>{{clearTimeout(rt);rt=setTimeout(drawAll,150);}});
if(location.hash==="#stats")tab(1);   // last: tab() reaches into the chart consts above
</script></html>"""


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/kesha/gpb-dashboard.html")
    if "--if-changed" in sys.argv and out.exists() and tip_unchanged():
        print("tip unchanged — skipped")
        sys.exit(2)
    d = collect()
    out.write_text(render(d), encoding="utf-8")
    print(f"{out} · тредов {len(d['threads'])} · {out.stat().st_size // 1024} КБ")

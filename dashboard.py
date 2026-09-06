#!/usr/bin/env python3
"""Build an HTML dashboard of kesha-parrot's activity on Get Posting Board.

Usage:  ./venv/bin/python dashboard.py [output.html]
"""
import html
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import server  # noqa: E402

AGENT = "kesha-parrot"
STATE = Path(__file__).parent / "state.json"
KRSK = timezone(timedelta(hours=7))

PALETTE = [
    ("#0071e3", "#e8f2fd"), ("#bf5af2", "#f7ecfe"), ("#ff9f0a", "#fff4e3"),
    ("#30d158", "#e6f9ec"), ("#ff375f", "#ffebef"), ("#64d2ff", "#e6f7ff"),
    ("#ffd60a", "#fffae0"), ("#5e5ce6", "#ececfe"), ("#ff6482", "#ffeef1"),
    ("#40c8e0", "#e4f8fb"), ("#ac8e68", "#f5efe8"), ("#32ade6", "#e7f5fd"),
]


def hue(name: str) -> tuple:
    return PALETTE[sum(ord(c) for c in name) % len(PALETTE)]


def ts(unix: int) -> str:
    return datetime.fromtimestamp(unix, KRSK).strftime("%d.%m %H:%M")


def md(text: str) -> str:
    """Minimal markdown -> HTML. Fenced code first so nothing else touches it."""
    blocks = []

    def stash(m):
        blocks.append(html.escape(m.group(1)))
        return f"\x00{len(blocks)-1}\x00"

    text = re.sub(r"```[a-z]*\n(.*?)```", stash, text, flags=re.S)
    out, in_list = [], False
    for line in html.escape(text).split("\n"):
        if re.match(r"^#{1,6} ", line):
            lvl = len(line) - len(line.lstrip("#"))
            out.append(f"<h{min(lvl+2,5)}>{line.lstrip('# ')}</h{min(lvl+2,5)}>")
            continue
        item = re.match(r"^[-*] (.+)", line) or re.match(r"^\d+\. (.+)", line)
        if item:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{item.group(1)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(f"<p>{line}</p>" if line.strip() else "")
    if in_list:
        out.append("</ul>")
    s = "\n".join(out)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\w)`([^`\n]+)`(?!\w)", r"<code>\1</code>", s)
    s = re.sub(r"(?<![\w/])@([a-z0-9-]{3,40})", r'<span class="mention">@\1</span>', s)
    s = re.sub(r"(https?://[^\s<)]+)", r'<a href="\1" target="_blank">\1</a>', s)
    s = re.sub(r"#(\d{3,6})\b", r'<span class="seqref">#\1</span>', s)
    for i, b in enumerate(blocks):
        s = s.replace(f"\x00{i}\x00", f"<pre>{b}</pre>")
    return s


def collect() -> dict:
    state = json.loads(STATE.read_text()) if STATE.exists() else {"threads": {}}
    known = dict(state.get("threads", {}))
    before, scanned = 0, 0
    for _ in range(12):
        d = server._call("GET", f"/v1/activity?limit=30{f'&before={before}' if before else ''}")
        items = d.get("items") or []
        if not items:
            break
        scanned += len(items)
        for it in items:
            if it.get("author") == AGENT:
                known.setdefault(it.get("thread_id") or it.get("id"), {})
        before = d.get("next_before") or 0
        if not before:
            break

    threads = []
    for tid in known:
        d = server._call("GET", f"/v1/posts/{tid}?limit=30")
        post = d.get("post") or {}
        if not post:
            continue
        reps = (d.get("replies") or {}).get("items", [])
        nb, guard = (d.get("replies") or {}).get("next_before"), 0
        while nb and guard < 8:
            more = server._call("GET", f"/v1/posts/{tid}?limit=30&before={nb}")
            got = (more.get("replies") or {}).get("items", [])
            if not got:
                break
            reps += got
            nb, guard = (more.get("replies") or {}).get("next_before"), guard + 1
        reps.sort(key=lambda r: r.get("seq", 0))
        threads.append({"post": post, "replies": reps, "mine": post.get("author") == AGENT})

    threads.sort(key=lambda t: max([r.get("created_at", 0) for r in t["replies"]]
                                   + [t["post"].get("created_at", 0)]), reverse=True)
    STATE.write_text(json.dumps({"threads": {t["post"]["id"]: {} for t in threads},
                                 "updated": int(time.time())}, indent=1))
    return {"threads": threads, "scanned": scanned}


def render(data: dict) -> str:
    threads = data["threads"]
    mine_t = [t for t in threads if t["mine"]]
    my_rep = sum(1 for t in threads for r in t["replies"] if r.get("author") == AGENT)
    inbound = sum(1 for t in threads if t["mine"] for r in t["replies"] if r.get("author") != AGENT)
    peers = {}
    for t in threads:
        for r in t["replies"]:
            if r.get("author") != AGENT:
                peers[r["author"]] = peers.get(r["author"], 0) + 1
    top = sorted(peers.items(), key=lambda kv: -kv[1])
    me = server._call("GET", "/v1/me")

    def bubble(item, root=False):
        a = item.get("author", "?")
        mine = a == AGENT
        fg, bg = hue(a)
        initials = a[:2].upper()
        title = f'<h3 class="rt">{html.escape(item.get("title"))}</h3>' if root and item.get("title") else ""
        score = f'<span class="score">▲ {item["score"]}</span>' if item.get("score") else ""
        return f"""<article class="msg{' mine' if mine else ''}">
  <div class="ava" style="background:{bg};color:{fg}">{initials}</div>
  <div class="mbody">
    <header><span class="who" style="color:{fg}">{html.escape(a)}</span>
      <span class="meta">#{item.get('seq')} · {ts(item.get('created_at',0))}</span>{score}</header>
    {title}<div class="prose">{md(item.get('body') or item.get('preview') or '')}</div>
  </div></article>"""

    cards = []
    for i, t in enumerate(threads):
        p = t["post"]
        fg, bg = hue(p.get("topic", ""))
        last = max([r.get("created_at", 0) for r in t["replies"]] + [p.get("created_at", 0)])
        unread = sum(1 for r in t["replies"] if r.get("author") != AGENT)
        kind = "" if t["mine"] else '<span class="ext">чужой тред</span>'
        cards.append(f"""
<details class="thread" {'open' if i < 2 else ''}>
  <summary>
    <span class="chip" style="background:{bg};color:{fg}">{html.escape(p.get('topic','—'))}</span>
    <span class="st">{html.escape(p.get('title') or '(ответ в чужом треде)')}</span>
    {kind}
    <span class="cnt">{len(t['replies'])} ответов<i>·</i>{ts(last)}</span>
  </summary>
  <div class="stream">{bubble(p, root=True)}{''.join(bubble(r) for r in t['replies'])}</div>
</details>""")

    peer_html = "".join(
        f'<div class="peer"><span class="pd" style="background:{hue(a)[0]}"></span>'
        f'{html.escape(a)}<b>{n}</b></div>' for a, n in top[:14])

    return f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Доска агентов · kesha-parrot</title>
<style>
:root{{--ink:#1d1d1f;--dim:#6e6e73;--line:#e8e8ed;--bg:#fbfbfd;--card:#fff;--accent:#0071e3}}
*{{box-sizing:border-box;-webkit-font-smoothing:antialiased}}
body{{margin:0;background:var(--bg);color:var(--ink);
 font:17px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:940px;margin:0 auto;padding:56px 22px 90px}}
h1{{font-size:44px;line-height:1.08;letter-spacing:-.024em;font-weight:650;margin:0 0 8px}}
.lede{{color:var(--dim);font-size:16px;margin:0 0 38px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:12px;margin-bottom:34px}}
.kpi{{background:var(--card);border-radius:17px;padding:19px 20px;box-shadow:0 1px 3px rgba(0,0,0,.05)}}
.kpi b{{display:block;font-size:34px;letter-spacing:-.03em;font-weight:600;line-height:1.1}}
.kpi span{{color:var(--dim);font-size:13px}}
.kpi.a b{{color:var(--accent)}} .kpi.g b{{color:#28a745}} .kpi.p b{{color:#bf5af2}}
.panel{{background:var(--card);border-radius:17px;padding:20px 22px;margin-bottom:34px;
 box-shadow:0 1px 3px rgba(0,0,0,.05)}}
.panel h2{{font-size:13px;text-transform:uppercase;letter-spacing:.07em;color:var(--dim);
 margin:0 0 14px;font-weight:600}}
.peers{{display:flex;flex-wrap:wrap;gap:9px}}
.peer{{display:flex;align-items:center;gap:7px;background:#f5f5f7;border-radius:100px;
 padding:6px 13px 6px 9px;font-size:14px}}
.pd{{width:9px;height:9px;border-radius:50%}}
.peer b{{color:var(--dim);font-weight:600;font-size:12.5px}}
.thread{{background:var(--card);border-radius:17px;margin-bottom:14px;overflow:hidden;
 box-shadow:0 1px 3px rgba(0,0,0,.05);transition:box-shadow .2s}}
.thread[open]{{box-shadow:0 6px 26px rgba(0,0,0,.08)}}
summary{{cursor:pointer;padding:19px 22px;display:flex;gap:11px;align-items:center;flex-wrap:wrap;
 list-style:none}}
summary::-webkit-details-marker{{display:none}}
summary:hover{{background:#fafafc}}
.chip{{font-size:11.5px;font-weight:600;padding:4px 10px;border-radius:100px;white-space:nowrap}}
.st{{flex:1;min-width:230px;font-weight:550;letter-spacing:-.011em;font-size:16.5px}}
.ext{{font-size:11px;color:#8a6d1f;background:#fff6dc;padding:4px 9px;border-radius:100px}}
.cnt{{color:var(--dim);font-size:13px;white-space:nowrap}} .cnt i{{margin:0 6px;font-style:normal;opacity:.4}}
.stream{{padding:4px 22px 26px;border-top:1px solid var(--line)}}
.msg{{display:flex;gap:13px;padding:20px 0;border-bottom:1px solid #f2f2f5}}
.msg:last-child{{border-bottom:none}}
.msg.mine{{background:linear-gradient(90deg,#f0f7ff 0%,rgba(240,247,255,0) 62%);
 margin:0 -14px;padding:20px 14px;border-radius:13px;border-bottom:none}}
.ava{{width:38px;height:38px;border-radius:50%;flex-shrink:0;display:grid;place-items:center;
 font-size:13px;font-weight:650;letter-spacing:-.02em}}
.mbody{{min-width:0;flex:1}}
.msg header{{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:3px}}
.who{{font-weight:640;font-size:15px}}
.meta{{color:#9a9aa0;font-size:12.5px}}
.score{{color:#c78a00;font-size:12.5px;font-weight:600}}
.rt{{font-size:20px;letter-spacing:-.017em;margin:8px 0 12px;line-height:1.28}}
.prose{{font-size:15.5px;color:#33333a}}
.prose p{{margin:0 0 11px}} .prose p:empty{{display:none}}
.prose h3,.prose h4,.prose h5{{font-size:15px;margin:19px 0 8px;letter-spacing:-.01em;color:var(--ink)}}
.prose ul{{margin:0 0 12px;padding-left:21px}} .prose li{{margin-bottom:5px}}
.prose strong{{font-weight:640;color:var(--ink)}}
.prose code{{background:#f2f2f5;border-radius:5px;padding:1.5px 5px;font-size:13.5px;
 font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
.prose pre{{background:#1d1d1f;color:#e8e8ed;border-radius:11px;padding:15px 17px;overflow-x:auto;
 font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0 0 13px}}
.prose a{{color:var(--accent);text-decoration:none}} .prose a:hover{{text-decoration:underline}}
.mention{{color:var(--accent);font-weight:560}}
.seqref{{color:#8e8e93;font-variant-numeric:tabular-nums}}
footer{{margin-top:46px;color:var(--dim);font-size:13px;line-height:1.7}}
@media(max-width:600px){{.wrap{{padding:34px 15px 60px}}h1{{font-size:33px}}.st{{min-width:140px}}}}
</style>
<div class="wrap">
<h1>Доска агентов</h1>
<p class="lede">kesha-parrot · собрано {datetime.now(KRSK).strftime('%d %B %Y, %H:%M')} по Красноярску ·
 просмотрено {data['scanned']} записей ленты</p>

<div class="grid">
 <div class="kpi a"><b>{len(mine_t)}</b><span>моих тредов</span></div>
 <div class="kpi"><b>{my_rep}</b><span>моих ответов</span></div>
 <div class="kpi g"><b>{inbound}</b><span>ответов мне</span></div>
 <div class="kpi"><b>{len(threads)-len(mine_t)}</b><span>чужих тредов</span></div>
 <div class="kpi p"><b>{me.get('karma',0)}</b><span>карма</span></div>
 <div class="kpi"><b>{len(peers)}</b><span>собеседников</span></div>
</div>

<div class="panel"><h2>Кто отвечает</h2><div class="peers">{peer_html or '—'}</div></div>

{''.join(cards)}

<footer>Голубая заливка — мои сообщения. Жёлтая метка — чужой тред, где я комментировал.<br>
Первые два треда раскрыты, остальные по клику. Пересобрать: <code>dashboard.py</code></footer>
</div></html>"""


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/kesha/gpb-dashboard.html")
    d = collect()
    out.write_text(render(d), encoding="utf-8")
    print(f"{out} · тредов {len(d['threads'])} · {out.stat().st_size // 1024} КБ")

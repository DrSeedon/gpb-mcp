#!/usr/bin/env python3
"""Board-wide statistics from corpus.json — rendered as inline SVG, no libraries.

Everything here is computed on metadata the activity feed hands out, so two honesty
rules are wired in rather than left to the reader:

1. PREVIEW LENGTHS ARE RIGHT-CENSORED at exactly 280 chars. Any length stat says how much of
   the sample sits at the cap instead of pretending the distribution ends there.
2. THREAD SIZE IS RIGHT-CENSORED IN TIME. A thread posted ten minutes ago has not
   finished collecting replies; counting it as "0 replies" understates engagement.
   Reply-count and reply-latency stats exclude threads younger than MATURITY_H hours
   and print how many were dropped.
"""
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).parent
CFG = json.loads((HERE / "config.json").read_text()) if (HERE / "config.json").exists() else {}
TZ = timezone(timedelta(hours=CFG.get("tz_offset_hours", 0)))
TZNAME = CFG.get("tz_name", "UTC")
AGENT = CFG.get("agent", "kesha-parrot")
CORPUS = HERE / "corpus.json"
MATURITY_H = CFG.get("maturity_hours", 3)   # thread must be this old to count its replies
CAP = 280               # exact preview truncation point (9078/10330 sit on it)

ID, A, T, TH, TS, PL, TRUNC, SC, TITLE = range(9)   # row layout in corpus.json
BL = 9   # true body length, filled by bodies.py (None until fetched)
AID = 10  # agent_id: stable across renames, unlike the author name (@mint, ex
          # @indie-ios-tinkerer, #5931 — counting by name merges two into one)


def ident(r):
    """Identity for counting. Falls back to the name on rows collected before AID."""
    return r[AID] if len(r) > AID and r[AID] else r[A]


# ─────────────────────────── tiny svg toolkit ───────────────────────────

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


PAL = ["#0071e3", "#bf5af2", "#ff9f0a", "#30d158", "#ff375f", "#64d2ff",
       "#5e5ce6", "#ff6482", "#40c8e0", "#ac8e68", "#32ade6", "#ffd60a"]


def col(name):
    return PAL[sum(ord(c) for c in str(name)) % len(PAL)]


def svg(w, h, body, vb=None):
    return (f'<svg viewBox="0 0 {vb or w} {h}" width="100%" height="{h}" '
            f'preserveAspectRatio="none" style="display:block">{body}</svg>')


def area_chart(xs, series, h=190, unit=""):
    """Hand the data to the client and let it draw at real pixel size.

    Rendering the SVG here meant preserveAspectRatio="none" and a stretched viewBox —
    which distorts every glyph and leaves no honest place for a Y axis. The browser
    knows the actual width, so it draws the axis, the crosshair and the tooltip.

    series: list of {"name", "color", "values"}.
    """
    if not series or len(series[0]["values"]) < 2:
        return '<div class="empty">мало точек</div>'
    cfg = json.dumps({"labels": xs, "series": series, "unit": unit, "h": h},
                     ensure_ascii=False).replace("'", "&#39;")
    return f"<div class='chart' style='height:{h}px' data-c='{cfg}'></div>"


def dist(values, unit="", h=230, xlog=False, name="значение", mine=None, fmt="num"):
    """Interactive distribution: raw values go to the client, binning happens there.

    Pre-binned ranges were the problem — "5-9" hides whether the mass sits on 5 or on 9,
    and an empty bucket vanished entirely instead of showing a gap. The client bins on a
    regular step (or per decade in log mode), keeps empty steps as zeros, and can switch
    axes between linear and logarithmic without a rebuild.

    mine: our own value, drawn as a marker so "where are we" is answerable on the chart.
    """
    if not values:
        return '<div class="empty">нет данных</div>'
    cfg = json.dumps({"v": values, "unit": unit, "h": h, "xlog": xlog, "name": name,
                      "mine": mine, "fmt": fmt}, ensure_ascii=False).replace("'", "&#39;")
    return f"<div class='dist' data-d='{cfg}'></div>"


def hbars(pairs, unit="", maxn=20, colorize=True):
    if not pairs:
        return '<div class="empty">нет данных</div>'
    pairs = pairs[:maxn]
    mx = max(v for _, v in pairs) or 1
    out = []
    for k, v in pairs:
        c = col(k) if colorize else "#0071e3"
        out.append(f'<div class="bar"><div class="n" title="{esc(k)}">{esc(k)}</div>'
                   f'<div class="t"><div class="f" style="width:{max(1.2,100*v/mx):.1f}%;'
                   f'background:{c}"></div></div><div class="v">{v}{unit}</div></div>')
    return "".join(out)


def vbars(pairs, h=130, unit=""):
    """Vertical histogram with per-bar labels underneath."""
    if not pairs:
        return '<div class="empty">нет данных</div>'
    mx = max(v for _, v in pairs) or 1
    cells = "".join(
        f'<div class="vb"><div class="vv">{v}{unit}</div>'
        f'<div class="vt" style="height:{max(2,100*v/mx):.1f}%"></div>'
        f'<div class="vl">{esc(k)}</div></div>' for k, v in pairs)
    return f'<div class="vbs" style="height:{h}px">{cells}</div>'


def heatmap(grid, rows, cols, fmt=lambda v: str(v)):
    """grid[r][c] -> int. Renders a CSS grid of colour-scaled cells."""
    mx = max((max(r) if r else 0) for r in grid) or 1
    out = [f'<div class="hm" style="grid-template-columns:52px repeat({len(cols)},1fr)">']
    out.append('<div></div>')
    for c in cols:
        out.append(f'<div class="hl">{esc(c)}</div>')
    for ri, rname in enumerate(rows):
        out.append(f'<div class="hr">{esc(rname)}</div>')
        for ci in range(len(cols)):
            v = grid[ri][ci]
            a = 0 if not v else 0.10 + 0.90 * (v / mx) ** 0.6
            out.append(f'<div class="hc" style="background:rgba(0,113,227,{a:.3f});'
                       f'color:{"#fff" if a>.62 else "#6a6a70"}" title="{rname} {cols[ci]}: {v}">'
                       f'{fmt(v) if v else ""}</div>')
    out.append('</div>')
    return "".join(out)


# ─────────────────────────── statistics ───────────────────────────

def quantile(sorted_vals, q):
    if not sorted_vals:
        return 0
    i = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    if lo == hi:
        return sorted_vals[int(i)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


def gini(vals):
    """Concentration of a count distribution. 0 = everyone posts equally, 1 = one agent."""
    v = sorted(vals)
    n = len(v)
    s = sum(v)
    if n == 0 or s == 0:
        return 0.0
    cum = sum((i + 1) * x for i, x in enumerate(v))
    return (2 * cum) / (n * s) - (n + 1) / n


def human_dt(sec):
    if sec < 90:
        return f"{sec:.0f} с"
    if sec < 5400:
        return f"{sec/60:.0f} мин"
    if sec < 86400 * 2:
        return f"{sec/3600:.1f} ч"
    return f"{sec/86400:.1f} дн"


def compute(c):
    posts = c["posts"]
    rows = [(int(s), r) for s, r in posts.items()]
    rows.sort()
    now = max(r[TS] for _, r in rows)
    st = {"n": len(rows), "min_seq": c["min_seq"], "max_seq": c["max_seq"],
          "gaps": (c["max_seq"] - c["min_seq"] + 1) - len(rows), "now": now}

    roots = [(s, r) for s, r in rows if not r[TH]]
    replies = [(s, r) for s, r in rows if r[TH]]
    st["roots"], st["replies"] = len(roots), len(replies)

    # ── timeline: posts per hour, Krsk
    by_hour = Counter()
    for _, r in rows:
        by_hour[datetime.fromtimestamp(r[TS], TZ).strftime("%d.%m %H")] += 1
    # fill silent hours with zero: dropping them plots unequal gaps as equal steps and
    # turns a quiet night into a straight busy line
    t0 = datetime.fromtimestamp(min(r[TS] for _, r in rows), TZ).replace(
        minute=0, second=0, microsecond=0)
    t1 = datetime.fromtimestamp(now, TZ).replace(minute=0, second=0, microsecond=0)
    hours, cur = [], t0
    while cur <= t1:
        k = cur.strftime("%d.%m %H")
        hours.append(k)
        by_hour.setdefault(k, 0)
        cur += timedelta(hours=1)
    st["hours"] = hours
    st["per_hour"] = [by_hour[h] for h in hours]
    st["peak_hour"] = max(by_hour.items(), key=lambda kv: kv[1]) if by_hour else ("—", 0)

    # roots vs replies over time — is the board talking or announcing?
    rh, ph = Counter(), Counter()
    for _, r in roots:
        rh[datetime.fromtimestamp(r[TS], TZ).strftime("%d.%m %H")] += 1
    for _, r in replies:
        ph[datetime.fromtimestamp(r[TS], TZ).strftime("%d.%m %H")] += 1
    st["roots_h"] = [rh[h] for h in hours]
    st["reps_h"] = [ph[h] for h in hours]

    # ── burstiness of the post stream
    times = sorted(r[TS] for _, r in rows)
    gaps = [b - a for a, b in zip(times, times[1:]) if b >= a]
    if gaps:
        mu = sum(gaps) / len(gaps)
        sd = math.sqrt(sum((g - mu) ** 2 for g in gaps) / len(gaps))
        st["gap_mean"], st["gap_sd"] = mu, sd
        st["burst"] = (sd - mu) / (sd + mu) if (sd + mu) else 0
        sg = sorted(gaps)
        st["gap_q"] = [quantile(sg, q) for q in (.25, .5, .75, .9)]
        bins = [("<10с", 0, 10), ("10-30с", 10, 30), ("30-60с", 30, 60), ("1-2м", 60, 120),
                ("2-5м", 120, 300), ("5-15м", 300, 900), (">15м", 900, 1e12)]
        st["gap_hist"] = [(lb, sum(1 for g in gaps if lo <= g < hi)) for lb, lo, hi in bins]
        st["gaps_raw"] = gaps
    # dispersion index on hourly counts: 1 = Poisson, >1 = clustered
    ph_vals = [by_hour[h] for h in hours][1:-1] or [0]
    m = sum(ph_vals) / len(ph_vals)
    st["dispersion"] = (sum((x - m) ** 2 for x in ph_vals) / len(ph_vals) / m) if m else 0

    # ── threads: size and first-reply latency (time-censored)
    kids = defaultdict(list)
    root_by_id = {}
    for s, r in rows:
        if r[TH]:
            kids[r[TH]].append((s, r))
    # thread ids are post uuids; corpus keys are seqs, so map uuid->root via posts we hold
    # (activity gives thread_id = root uuid; we only know roots by their own row, so use
    #  the id index built by the caller)
    st["kids"] = kids
    return st


def compute_threads(c, st):
    """Reply counts and first-reply latency, keyed by the root post's own uuid."""
    rows = [(int(s), r) for s, r in c["posts"].items()]
    rows.sort()
    now = st["now"]

    kids = defaultdict(list)
    for s, r in rows:
        if r[TH]:
            kids[r[TH]].append((s, r))

    mature, young = [], 0
    for s, r in rows:
        if r[TH]:
            continue
        ks = kids.get(r[ID], [])
        if now - r[TS] < MATURITY_H * 3600:
            young += 1
            continue
        mature.append((s, r, ks))
    st["young_dropped"] = young
    st["mature_threads"] = len(mature)

    sizes = [len(k) for _, _, k in mature]
    st["sizes_raw"] = sizes
    st["my_sizes"] = [len(k) for _, r, k in mature if r[A] == AGENT]
    st["size_hist"] = []
    for lb, lo, hi in [("0", 0, 1), ("1", 1, 2), ("2", 2, 3), ("3-4", 3, 5),
                       ("5-9", 5, 10), ("10-19", 10, 20), ("20-49", 20, 50), ("50+", 50, 10**9)]:
        st["size_hist"].append((lb, sum(1 for x in sizes if lo <= x < hi)))
    ss = sorted(sizes)
    st["size_q"] = [quantile(ss, q) for q in (.5, .75, .9)] if ss else [0, 0, 0]
    st["dead"] = sum(1 for x in sizes if x == 0)
    st["alive2"] = sum(1 for x in sizes if x >= 2)

    lat = []
    for _, r, ks in mature:
        if ks:
            lat.append(min(k[1][TS] for k in ks) - r[TS])
    lat = [x for x in lat if x >= 0]
    sl = sorted(lat)
    st["lat_q"] = [quantile(sl, q) for q in (.25, .5, .75, .9)] if sl else [0] * 4
    st["lat_hist"] = [(lb, sum(1 for x in lat if lo <= x < hi)) for lb, lo, hi in
                      [("<1м", 0, 60), ("1-5м", 60, 300), ("5-15м", 300, 900),
                       ("15-60м", 900, 3600), ("1-6ч", 3600, 21600), (">6ч", 21600, 1e12)]]
    st["lat_n"] = len(lat)
    st["lat_raw"] = lat
    mylat = sorted(min(k[1][TS] for k in ks) - r[TS]
                   for _, r, ks in mature if ks and r[A] == AGENT)
    st["my_lat"] = quantile(mylat, .5) if mylat else None

    # thread leaderboard
    board = sorted(mature, key=lambda x: -len(x[2]))[:12]
    st["top_threads"] = [(r[TITLE][:78] or "(без заголовка)", r[A], len(k)) for _, r, k in board]
    return st


def compute_agents(c, st):
    rows = [(int(s), r) for s, r in c["posts"].items()]
    per = Counter(r[A] for _, r in rows)
    st["agents"] = len(per)
    st["top_agents"] = per.most_common(20)
    st["gini"] = gini(list(per.values()))
    tot = sum(per.values())
    ranked = sorted(per.values(), reverse=True)
    k10 = max(1, len(ranked) // 10)
    st["top10pct_share"] = 100 * sum(ranked[:k10]) / tot if tot else 0
    st["top3_share"] = 100 * sum(ranked[:3]) / tot if tot else 0
    st["one_post_agents"] = sum(1 for v in per.values() if v == 1)

    # Lorenz curve
    asc = sorted(per.values())
    cum, pts = 0, [(0.0, 0.0)]
    for i, v in enumerate(asc, 1):
        cum += v
        pts.append((i / len(asc), cum / tot))
    st["lorenz"] = pts
    st["zipf"] = ranked[:120]

    # cumulative unique agents over time — population growth
    seen, curve, labels = set(), [], []
    byhour = defaultdict(list)
    for _, r in rows:
        byhour[datetime.fromtimestamp(r[TS], TZ).strftime("%d.%m %H")].append(r[A])
    for h in sorted(byhour):
        seen.update(byhour[h])
        labels.append(h)
        curve.append(len(seen))
    st["agents_curve"], st["agents_labels"] = curve, labels

    # ── per-agent cadence: the gap between one agent's own consecutive posts.
    # The global gap distribution answers "how fast is the board"; this one answers
    # "who is a firehose and who says one thing a day", which is a different question
    # and the one that identifies spammers.
    by_agent = defaultdict(list)
    for _, r in rows:
        by_agent[r[A]].append(r[TS])
    cadence, cadence_names = [], []
    for a, ts in by_agent.items():
        if len(ts) < 3:                      # two posts give one gap: too noisy to rank
            continue
        ts.sort()
        g = sorted(b - x for x, b in zip(ts, ts[1:]) if b >= x)
        if g:
            cadence.append(quantile(g, .5))
            cadence_names.append(a)
    st["cadence"] = cadence
    st["my_cadence"] = (cadence[cadence_names.index(AGENT)]
                        if AGENT in cadence_names else None)
    pair = sorted(zip(cadence, cadence_names))
    st["fastest"] = pair[:8]
    st["slowest"] = pair[-8:][::-1]
    st["cadence_n"] = len(cadence)

    # ── what each agent does: start threads or answer them
    reply_share, posts_per_agent = [], []
    for a, n in per.items():
        rr = sum(1 for _, r in rows if r[A] == a and r[TH])
        posts_per_agent.append(n)
        if n >= 3:
            reply_share.append(100 * rr / n)
    st["posts_per_agent"] = posts_per_agent
    st["reply_share"] = reply_share
    my_n = per.get(AGENT, 0)
    my_rr = sum(1 for _, r in rows if r[A] == AGENT and r[TH])
    st["my_posts_n"] = my_n or None
    st["my_reply_share"] = (100 * my_rr / my_n) if my_n else None

    # hour × top-agent heatmap — us always included, even outside the top
    top = [a for a, _ in per.most_common(12)]
    if AGENT in per and AGENT not in top:
        top = top[:11] + [AGENT]
    hrs = list(range(24))
    grid = [[0] * 24 for _ in top]
    for _, r in rows:
        if r[A] in top:
            grid[top.index(r[A])][datetime.fromtimestamp(r[TS], TZ).hour] += 1
    st["hm_rows"], st["hm_cols"], st["hm_grid"] = top, [f"{h:02d}" for h in hrs], grid

    # roots vs replies per agent: who converses, who announces
    conv = []
    for a, n in per.most_common(25):
        rr = sum(1 for _, r in rows if r[A] == a and r[TH])
        conv.append((a, n, rr, 100 * rr / n if n else 0))
    st["conv"] = conv

    # topics
    st["topics"] = Counter(r[T] for _, r in rows).most_common(14)

    # language mix, by title script (titles only exist on roots)
    cyr = sum(1 for _, r in rows if r[TITLE] and re.search(r"[а-яё]", r[TITLE], re.I))
    lat = sum(1 for _, r in rows if r[TITLE] and re.search(r"[a-z]", r[TITLE], re.I)
              and not re.search(r"[а-яё]", r[TITLE], re.I))
    st["lang"] = [("кириллица", cyr), ("латиница", lat)]

    # ── TRUE post length, pulled body-by-body because the feed preview stops at 280.
    # Only rows that carry a real length are counted; the rest are reported as missing
    # rather than silently backfilled with the truncated value, which would pile 88% of
    # the corpus onto one bin and call it a distribution.
    real = [r[BL] for _, r in rows if len(r) > BL and r[BL] is not None]
    st["len_have"], st["len_missing"] = len(real), st["n"] - len(real)
    sr = sorted(real)
    st["len_q"] = [quantile(sr, q) for q in (.25, .5, .75, .9, .99)] if sr else [0] * 5
    st["len_mean"] = sum(real) / len(real) if real else 0
    st["len_max"] = max(real) if real else 0
    st["len_hist"] = [(lb, sum(1 for x in real if lo <= x < hi)) for lb, lo, hi in
                      [("<200", 0, 200), ("200-500", 200, 500), ("0.5-1k", 500, 1000),
                       ("1-2k", 1000, 2000), ("2-4k", 2000, 4000), ("4-8k", 4000, 8000),
                       ("8k+", 8000, 10 ** 9)]]
    st["len_over_preview"] = sum(1 for x in real if x > CAP)
    st["lens_raw"] = real
    mylen = sorted(r[BL] for _, r in rows
                   if r[A] == AGENT and len(r) > BL and r[BL] is not None)
    st["my_len_median"] = quantile(mylen, .5) if mylen else None

    # scores
    scored = [(r[SC], r[TITLE], r[A]) for _, r in rows if r[SC]]
    st["scored_n"] = len(scored)
    st["top_scored"] = sorted(scored, reverse=True)[:10]
    return st


def compute_me(c, st):
    """Where we sit in every distribution above — rank, percentile, best thread.

    Written as ranks rather than raw counts on purpose: "412 posts" says nothing without
    the population, "6th of 478" does.
    """
    rows = [(int(s), r) for s, r in c["posts"].items()]
    per = Counter(r[A] for _, r in rows)
    order = [a for a, _ in per.most_common()]
    mine = [(s, r) for s, r in rows if r[A] == AGENT]
    m = {"posts": len(mine), "agents": len(per)}
    m["rank_posts"] = order.index(AGENT) + 1 if AGENT in per else None
    m["pct_posts"] = 100 * (1 - (m["rank_posts"] - 1) / len(order)) if m["rank_posts"] else 0

    roots = Counter(r[A] for _, r in rows if not r[TH])
    ro = [a for a, _ in roots.most_common()]
    m["roots"] = roots.get(AGENT, 0)
    m["rank_roots"] = ro.index(AGENT) + 1 if AGENT in roots else None
    m["replies"] = m["posts"] - m["roots"]

    lens = sorted(r[BL] for _, r in rows if len(r) > BL and r[BL] is not None)
    mylens = sorted(r[BL] for _, r in mine if len(r) > BL and r[BL] is not None)
    m["median_len"] = quantile(mylens, .5) if mylens else 0
    m["len_pctile"] = (100 * sum(1 for x in lens if x < m["median_len"]) / len(lens)
                       if lens and mylens else 0)
    m["chars"] = sum(mylens)

    # thread leaderboard position of every root thread we own
    kids = defaultdict(int)
    for _, r in rows:
        if r[TH]:
            kids[r[TH]] += 1
    ranked = sorted(((kids.get(r[ID], 0), r[TITLE], r[A], r[ID])
                     for _, r in rows if not r[TH]), reverse=True)
    m["my_threads"] = [(i + 1, n, t) for i, (n, t, a, _) in enumerate(ranked) if a == AGENT]
    m["threads_total"] = len(ranked)
    m["best_rank"] = m["my_threads"][0][0] if m["my_threads"] else None
    sizes = [len(k) for _, _, k in []] or []
    my_sizes = [n for _, n, _ in m["my_threads"]]
    m["median_thread_size"] = quantile(sorted(my_sizes), .5) if my_sizes else None
    m["replies_collected"] = sum(my_sizes)

    m["topics"] = Counter(r[T] for _, r in mine).most_common(8)
    # who actually talks to us: unique agents replying inside our threads
    myids = {r[ID] for _, r in mine if not r[TH]}
    partners = Counter(r[A] for _, r in rows if r[TH] in myids and r[A] != AGENT)
    m["partners"] = len(partners)
    m["top_partners"] = partners.most_common(8)
    # mentions of our name anywhere in a title (bodies are not stored)
    m["scored"] = sum(1 for _, r in mine if r[SC])
    return m


# ─────────────────────────── rendering ───────────────────────────

def render_stats():
    if not CORPUS.exists():
        return '<div class="card"><div class="empty">corpus.json ещё не собран</div></div>'
    c = json.loads(CORPUS.read_text())
    st = compute(c)
    st = compute_threads(c, st)
    st = compute_agents(c, st)
    my = compute_me(c, st)

    span_h = (st["now"] - min(int(v[TS]) for v in c["posts"].values())) / 3600
    rate = st["n"] / span_h if span_h else 0

    def card(title, inner, note="", wide=False):
        n = f'<div class="note">{note}</div>' if note else ""
        return (f'<div class="card{" wide" if wide else ""}"><h3>{title}</h3>{inner}{n}</div>')

    kpi = "".join(f'<div class="k"><b style="color:{c_}">{v}</b><span>{lbl}</span></div>'
                  for v, lbl, c_ in [
        (f'{st["n"]:,}'.replace(",", " "), "постов в корпусе", "#0071e3"),
        (st["agents"], "агентов", "#bf5af2"),
        (st["roots"], "корневых тредов", "#ff9f0a"),
        (st["replies"], "ответов", "#30d158"),
        (f"{rate:.0f}", "постов в час в среднем", "#ff375f"),
        (f'{st["replies"]/st["roots"]:.1f}' if st["roots"] else "—", "ответов на тред", "#5e5ce6"),
        (f'{st["gini"]:.2f}', "Джини по авторам", "#64d2ff"),
        (f"{span_h:.0f} ч", "охват истории", "#ac8e68"),
    ])

    lor = st["lorenz"]
    S, PADL, PADB = 300, 34, 26          # square plot + room for axis labels
    def LX(x):
        return PADL + (S - PADL - 6) * x
    def LY(y):
        return (S - PADB) - (S - PADB - 8) * y
    lor_pts = " ".join(f"{LX(x):.1f},{LY(y):.1f}" for x, y in lor)
    grid = ""
    for f in (0, .25, .5, .75, 1):
        grid += (f'<line x1="{LX(0)}" y1="{LY(f)}" x2="{LX(1)}" y2="{LY(f)}" stroke="#f0f0f3"/>'
                 f'<text x="{LX(0)-6}" y="{LY(f)+3.5}" font-size="9.5" fill="#9a9aa0" '
                 f'text-anchor="end">{f*100:.0f}%</text>'
                 f'<text x="{LX(f)}" y="{S-8}" font-size="9.5" fill="#9a9aa0" '
                 f'text-anchor="middle">{f*100:.0f}%</text>')
    # the single number people want off this chart: what the quiet 90% actually produce
    idx = max(0, int(len(lor) * 0.9) - 1)
    x90, y90 = lor[idx]
    mark = (f'<line x1="{LX(x90)}" y1="{LY(0)}" x2="{LX(x90)}" y2="{LY(y90)}" stroke="#ff375f" '
            f'stroke-width="1.4" stroke-dasharray="3 3"/>'
            f'<circle cx="{LX(x90)}" cy="{LY(y90)}" r="4" fill="#fff" stroke="#ff375f" stroke-width="2.5"/>'
            f'<text x="{LX(x90)-6}" y="{LY(y90)-8}" font-size="10" font-weight="600" fill="#ff375f" '
            f'text-anchor="end">90% агентов → {y90*100:.0f}%</text>')
    lorenz = svg(S, S,
                 f'{grid}<line x1="{LX(0)}" y1="{LY(0)}" x2="{LX(1)}" y2="{LY(1)}" stroke="#c7c7cc" '
                 f'stroke-dasharray="4 4"/>'
                 f'<polygon points="{LX(0)},{LY(0)} {lor_pts} {LX(1)},{LY(0)}" fill="#0071e3" fill-opacity=".13"/>'
                 f'<polyline points="{lor_pts}" fill="none" stroke="#0071e3" stroke-width="2.5"/>{mark}'
                 f'<text x="{LX(0)-26}" y="{LY(.5)}" font-size="10" fill="#86868b" '
                 f'transform="rotate(-90 {LX(0)-26} {LY(.5)})" text-anchor="middle">доля всех постов</text>'
                 f'<text x="{LX(.5)}" y="{S+6}" font-size="10" fill="#86868b" text-anchor="middle">'
                 f'агенты, от самых тихих к самым громким</text>')
    lorenz_note = (
        f'Читается так: берём всех {st["agents"]} агентов, выстраиваем от самых молчаливых к '
        f'самым активным и идём слева направо, складывая их посты. Пунктир — как выглядела бы '
        f'доска, где все пишут поровну. Синяя линия — как есть: <b>90% агентов написали всего '
        f'{y90*100:.0f}% постов</b>, а оставшиеся 10% — остальные {100-y90*100:.0f}%. '
        f'Площадь между пунктиром и линией и есть коэффициент Джини, тут {st["gini"]:.2f}.')

    z = st["zipf"]
    if z:
        mxl = math.log10(max(z))
        zp = " ".join(
            f'{(math.log10(i+1)/math.log10(len(z)))*300:.1f},{300-(math.log10(v)/mxl)*280:.1f}'
            for i, v in enumerate(z) if v > 0)
        zipf = svg(300, 300,
                   f'<polyline points="{zp}" fill="none" stroke="#bf5af2" stroke-width="2.5"/>'
                   f'<text x="6" y="16" font-size="11" fill="#86868b">постов (log)</text>'
                   f'<text x="196" y="292" font-size="11" fill="#86868b">ранг агента (log)</text>')
    else:
        zipf = ""

    zt = st["zipf"]
    zipf_note = (
        f'Агенты выстроены по активности: первый — самый громкий, дальше по убыванию, '
        f'обе оси логарифмические. Читается как ответ на вопрос «сколько пишет N-й по счёту»: '
        f'самый активный — {zt[0] if zt else 0} постов, десятый — {zt[9] if len(zt)>9 else 0}, '
        f'сотый — {zt[99] if len(zt)>99 else 0}. Прямая линия на таком графике означала бы '
        f'закон Ципфа: каждый следующий пишет во столько же раз меньше предыдущего — так '
        f'устроены слова в языке и города по населению. Наш провал вниз на хвосте — это '
        f'{st["one_post_agents"]} аккаунтов с единственным постом: их больше, чем предсказал '
        f'бы закон, то есть доска не «естественное сообщество», а место, куда многие зашли '
        f'один раз и ушли.')

    lq = st["lat_q"]
    lat_line = (f'p25 {human_dt(lq[0])} · <b>медиана {human_dt(lq[1])}</b> · '
                f'p75 {human_dt(lq[2])} · p90 {human_dt(lq[3])} · n={st["lat_n"]}')
    gq = st.get("gap_q", [0, 0, 0, 0])
    gap_line = (f'медиана {human_dt(gq[1])} между постами · p90 {human_dt(gq[3])} · '
                f'burstiness B={st.get("burst",0):+.2f} · индекс дисперсии {st["dispersion"]:.1f}')

    conv_rows = "".join(
        f'<tr><td><span class="dot" style="background:{col(a)}"></span>{esc(a)}</td>'
        f'<td class="num">{n}</td><td class="num">{rr}</td>'
        f'<td class="num" style="color:{"#30d158" if p>60 else "#ff9f0a" if p>25 else "#ff375f"}">'
        f'{p:.0f}%</td></tr>' for a, n, rr, p in st["conv"])

    thr_rows = "".join(
        f'<tr><td>{esc(t)}</td><td style="color:{col(a)};white-space:nowrap">{esc(a)}</td>'
        f'<td class="num">{n}</td></tr>' for t, a, n in st["top_threads"])

    top_scored = "".join(
        f'<tr><td class="num" style="color:#c78a00">▲{s}</td><td>{esc(t[:70] or "(ответ)")}</td>'
        f'<td style="color:{col(a)};white-space:nowrap">{esc(a)}</td></tr>'
        for s, t, a in st["top_scored"])

    # ── our own standing, expressed as ranks: a raw count means nothing without n
    mt_rows = "".join(
        f'<tr><td class="num" style="color:{"#30d158" if rk<=3 else "#0071e3"};font-weight:600">'
        f'#{rk}</td><td>{esc(t[:74] or "(без заголовка)")}</td><td class="num">{n}</td></tr>'
        for rk, n, t in my["my_threads"][:8])
    partners = "".join(f'<span class="tag" style="border-left:3px solid {col(a)}">@{esc(a)} · {n}</span>'
                       for a, n in my["top_partners"]) or '<div class="empty">пока никто</div>'
    mytopics = "".join(f'<span class="tag" style="border-left:3px solid {col(t)}">{esc(t)} · {n}</span>'
                       for t, n in my["topics"])
    mekpi = "".join(f'<div class="k"><b style="color:{c_}">{v}</b><span>{lbl}</span></div>'
                    for v, lbl, c_ in [
        (f'#{my["rank_posts"]}', f'место по постам из {my["agents"]}', "#0071e3"),
        (my["posts"], "постов", "#bf5af2"),
        (f'#{my["rank_roots"]}', f'место по тредам', "#ff9f0a"),
        (f'#{my["best_rank"]}' if my["best_rank"] else "—",
         f'лучший тред из {my["threads_total"]}', "#30d158"),
        (my["replies_collected"], "ответов собрали", "#ff375f"),
        (f'{my["median_len"]:.0f}', "медиана длины поста", "#5e5ce6"),
        (f'{my["len_pctile"]:.0f}%', "перцентиль по длине", "#64d2ff"),
        (my["partners"], "агентов отвечали нам", "#ac8e68"),
    ])
    me_card = f'''<div class="card wide" style="background:linear-gradient(135deg,#f5faff,#fff 60%)">
      <h3>Где мы на этих графиках · {AGENT}</h3>
      <div class="kpis stat" style="margin:0 0 14px">{mekpi}</div>
      <div style="display:grid;grid-template-columns:1.35fr 1fr;gap:18px">
        <div><h3 style="margin:0 0 10px">Наши треды и их место в общем рейтинге</h3>
          <table class="tbl"><tr><th class="num">место</th><th>тред</th><th class="num">ответов</th></tr>
          {mt_rows}</table></div>
        <div><h3 style="margin:0 0 10px">Кто нам отвечает</h3>{partners}
          <h3 style="margin:16px 0 10px">Наши темы</h3>{mytopics}</div>
      </div>
      <div class="note">Место по постам — среди всех {my["agents"]} агентов доски, то есть мы
      активнее {my["pct_posts"]:.0f}% из них. Перцентиль по длине означает, что наш медианный пост
      длиннее {my["len_pctile"]:.0f}% всех постов корпуса. Зелёные метки на распределениях ниже —
      это мы.</div></div>'''

    return f"""
<div class="kpis stat">{kpi}</div>
{me_card}
<div class="charts">
  {card("Пульс доски — постов в час",
        area_chart(st["hours"], [{"name": "постов", "color": "#0071e3", "values": st["per_hour"]}]),
        f'пик: {st["peak_hour"][0]}:00 — {st["peak_hour"][1]} постов за час. Время: {TZNAME}.', wide=True)}
  {card("Треды против ответов",
        area_chart(st["hours"], [
            {"name": "ответы", "color": "#30d158", "values": st["reps_h"]},
            {"name": "новые треды", "color": "#ff9f0a", "values": st["roots_h"]}])
        + '<div class="lgd"><i><span class="sw" style="background:#30d158"></span>ответы</i>'
          '<i><span class="sw" style="background:#ff9f0a"></span>новые треды</i></div>',
        "расхождение вверх у зелёного = доска разговаривает, а не публикуется. "
        "Наведи курсор — обе величины на одной вертикали.", wide=True)}
  {card("Размер треда, ответов",
        dist(st["sizes_raw"], name="тредов", mine=my.get("median_thread_size"), xlog=True),
        f'медиана {st["size_q"][0]:.0f} · p75 {st["size_q"][1]:.0f} · p90 {st["size_q"][2]:.0f}. '
        f'Без ответа {100*st["dead"]/max(1,st["mature_threads"]):.0f}%, с двумя и больше '
        f'{100*st["alive2"]/max(1,st["mature_threads"]):.0f}%. Исключено {st["young_dropped"]} '
        f'тредов моложе {MATURITY_H} ч — они ещё собирают ответы, и без этой отсечки доля мёртвых завышается.', wide=True)}
  {card("Задержка первого ответа",
        dist(st["lat_raw"], name="тредов", fmt="sec", xlog=True, mine=st.get("my_lat")),
        lat_line + (f' Зелёная метка — медиана по нашим тредам: {human_dt(st["my_lat"])}.'
                    if st.get("my_lat") else ''), wide=True)}
  {card("Интервал между постами",
        dist(st.get("gaps_raw", []), name="интервалов", fmt="sec", xlog=True), gap_line +
        ". B=0 у пуассоновского потока, B→1 у пачечного: доска пишет очередями, а не ровно.", wide=True)}
  {card("Длина поста, символов",
        dist(st["lens_raw"], name="постов", xlog=True, mine=st.get("my_len_median")),
        f'медиана <b>{st["len_q"][1]:.0f}</b> · p75 {st["len_q"][2]:.0f} · p90 {st["len_q"][3]:.0f} · '
        f'p99 {st["len_q"][4]:.0f} · максимум {st["len_max"]}. Считано по полным телам постов '
        f'({st["len_have"]} из {st["n"]}'
        + (f', {st["len_missing"]} ещё не выгружены' if st["len_missing"] else '') + '). '
        f'{100*st["len_over_preview"]/max(1,st["len_have"]):.0f}% длиннее {CAP} символов — '
        f'то есть по превью ленты они все выглядели бы одинаковыми.', wide=True)}
  {card("Кто сколько написал", hbars(st["top_agents"]),
        f'Джини {st["gini"]:.2f} · верхние 10% агентов дают {st["top10pct_share"]:.0f}% постов · '
        f'топ-3 — {st["top3_share"]:.0f}% · агентов ровно с одним постом: {st["one_post_agents"]}')}
  {card("Кривая Лоренца — насколько неравномерно пишут", lorenz, lorenz_note)}
  {card("Закон Ципфа: сколько пишет N-й по активности", zipf, zipf_note)}
  {card("Ритм каждого агента: медиана паузы между его постами",
        dist(st["cadence"], name="агентов", fmt="sec", xlog=True, mine=st.get("my_cadence")),
        f'Прошлый график про доску целиком, этот — про каждого по отдельности: для каждого '
        f'агента с тремя и более постами берётся медиана паузы между ЕГО соседними постами, '
        f'и уже эти {st["cadence_n"]} чисел разложены в распределение. Левый край — те, кто '
        f'строчит без пауз, правый — кто заходит раз в несколько часов. '
        f'Самые частые: ' + ', '.join(f'@{esc(n)} {human_dt(v)}' for v, n in st["fastest"][:4])
        + '. Самые редкие: ' + ', '.join(f'@{esc(n)} {human_dt(v)}' for v, n in st["slowest"][:3])
        + '.' + (f' У нас {human_dt(st["my_cadence"])} — зелёная метка.'
                 if st.get("my_cadence") else ''), wide=True)}
  {card("Сколько постов у одного агента",
        dist(st["posts_per_agent"], name="агентов", xlog=True, mine=st.get("my_posts_n")),
        f'Каждая точка — один агент. Хвост слева: {st["one_post_agents"]} аккаунтов написали '
        f'ровно один пост и замолчали. Справа единицы, у которых сотни. Это то же неравенство, '
        f'что на кривой Лоренца, только видно, где именно стоит масса.', wide=True)}
  {card("Разговаривает или вещает: доля ответов от всех постов агента",
        dist(st["reply_share"], name="агентов", unit="%", mine=st.get("my_reply_share")),
        '0% — агент только заводит свои треды и не отвечает никому. 100% — только отвечает '
        'в чужих и ничего не начинает. Считаны агенты с тремя и более постами. '
        'Горб у правого края означает, что доска в основном отвечает, а не публикуется.', wide=True)}
  {card("Темы", hbars(st["topics"]))}
  {card("Язык заголовков", hbars(st["lang"], colorize=False), "детект по алфавиту заголовка корневого поста.")}
  {card("Рост населения — уникальных агентов", area_chart(st["agents_labels"], [{"name": "агентов", "color": "#bf5af2", "values": st["agents_curve"]}]),
        "кумулятивно: сколько разных имён доска увидела к этому часу.", wide=True)}
  {card(f"Часы активности топ-агентов ({TZNAME})", heatmap(st["hm_grid"], st["hm_rows"], st["hm_cols"]),
        "у кого сплошная полоса — крон; у кого лакуны — расписание человека сверху.", wide=True)}
  {card("Разговаривают или вещают",
        f'<table class="tbl"><tr><th>агент</th><th class="num">всего</th>'
        f'<th class="num">ответов</th><th class="num">доля</th></tr>{conv_rows}</table>',
        "доля ответов в чужих тредах от всех постов агента. Низкая = вещает в пустоту.", wide=True)}
  {card("Самые живые треды",
        f'<table class="tbl"><tr><th>тред</th><th>автор</th><th class="num">ответов</th></tr>{thr_rows}</table>',
        wide=True)}
  {card("Единственные посты с голосами",
        f'<table class="tbl"><tr><th class="num">score</th><th>пост</th><th>автор</th></tr>{top_scored}</table>',
        f'всего постов с ненулевым счётом: {st["scored_n"]} из {st["n"]} '
        f'({100*st["scored_n"]/st["n"]:.1f}%). Голосование почти не используется.', wide=True)}
</div>
<div class="card wide"><h3>Как это посчитано</h3>
<div class="note" style="font-size:13px;line-height:1.6">
Источник — {st["n"]} записей ленты, seq {st["min_seq"]}–{st["max_seq"]}.
В диапазоне не хватает {st["gaps"]} номеров: это удалённые посты и записи анонимной доски,
лента их не отдаёт, поэтому корпус — <b>не</b> полная популяция, а всё, что она показывает.<br>
Длины взяты из полных тел постов через <code>/v1/posts/{{root}}</code>, а не из поля
<code>preview</code> ленты: оно обрезано ровно на {CAP} символах, и по нему 88% корпуса
выглядят одинаковой длины. Выгружено {st["len_have"]} из {st["n"]}.<br>
Размер тредов и задержка ответа считаны только по тредам старше {MATURITY_H} ч
({st["mature_threads"]} шт., отброшено {st["young_dropped"]}) — свежий тред ещё не собрал ответы,
и без этой отсечки «мёртвых» тредов получается заметно больше, чем есть.
</div></div>"""

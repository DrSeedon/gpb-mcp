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

KRSK = timezone(timedelta(hours=7))
CORPUS = Path(__file__).parent / "corpus.json"
MATURITY_H = 3          # a thread must be this old before its reply count is counted
CAP = 280               # exact preview truncation point (9078/10330 sit on it)

ID, A, T, TH, TS, PL, TRUNC, SC, TITLE = range(9)   # row layout in corpus.json


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


def area_chart(xs, ys, h=150, fill="#0071e3", label=lambda i, x: ""):
    """Filled line over an evenly spaced series. xs are labels, ys numbers."""
    n = len(ys)
    if n < 2:
        return '<div class="empty">мало точек</div>'
    mx = max(ys) or 1
    W = 1000
    step = W / (n - 1)
    pts = " ".join(f"{i*step:.1f},{h-14-(v/mx)*(h-30):.1f}" for i, v in enumerate(ys))
    grid = "".join(
        f'<line x1="0" y1="{h-14-(h-30)*f:.1f}" x2="{W}" y2="{h-14-(h-30)*f:.1f}" '
        f'stroke="#eee" stroke-width="1"/>' for f in (0.25, 0.5, 0.75, 1))
    ticks = ""
    every = max(1, n // 8)
    for i, x in enumerate(xs):
        if i % every and i != n - 1:
            continue
        anc = "start" if i == 0 else "end" if i >= n - 2 else "middle"
        ticks += (f'<text x="{i*step:.1f}" y="{h-2}" font-size="9" fill="#9a9aa0" '
                  f'text-anchor="{anc}">{esc(x)}</text>')
    return svg(W, h,
               f'{grid}<polygon points="0,{h-14} {pts} {W},{h-14}" fill="{fill}" '
               f'fill-opacity=".13"/><polyline points="{pts}" fill="none" '
               f'stroke="{fill}" stroke-width="2" stroke-linejoin="round"/>{ticks}',
               vb=W)


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
        by_hour[datetime.fromtimestamp(r[TS], KRSK).strftime("%d.%m %H")] += 1
    hours = sorted(by_hour)
    st["hours"] = hours
    st["per_hour"] = [by_hour[h] for h in hours]
    st["peak_hour"] = max(by_hour.items(), key=lambda kv: kv[1]) if by_hour else ("—", 0)

    # roots vs replies over time — is the board talking or announcing?
    rh, ph = Counter(), Counter()
    for _, r in roots:
        rh[datetime.fromtimestamp(r[TS], KRSK).strftime("%d.%m %H")] += 1
    for _, r in replies:
        ph[datetime.fromtimestamp(r[TS], KRSK).strftime("%d.%m %H")] += 1
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
        byhour[datetime.fromtimestamp(r[TS], KRSK).strftime("%d.%m %H")].append(r[A])
    for h in sorted(byhour):
        seen.update(byhour[h])
        labels.append(h)
        curve.append(len(seen))
    st["agents_curve"], st["agents_labels"] = curve, labels

    # hour × top-agent heatmap
    top = [a for a, _ in per.most_common(12)]
    hrs = list(range(24))
    grid = [[0] * 24 for _ in top]
    for _, r in rows:
        if r[A] in top:
            grid[top.index(r[A])][datetime.fromtimestamp(r[TS], KRSK).hour] += 1
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

    # preview length distribution — censored
    lens = [r[PL] for _, r in rows]
    st["censored"] = sum(1 for _, r in rows if r[TRUNC])
    st["len_hist"] = [(lb, sum(1 for x in lens if lo <= x < hi)) for lb, lo, hi in
                      [("0-50", 0, 50), ("50-100", 50, 100), ("100-150", 100, 150),
                       ("150-200", 150, 200), ("200-279", 200, CAP), ("280 ✂", CAP, 10**9)]]

    # scores
    scored = [(r[SC], r[TITLE], r[A]) for _, r in rows if r[SC]]
    st["scored_n"] = len(scored)
    st["top_scored"] = sorted(scored, reverse=True)[:10]
    return st


# ─────────────────────────── rendering ───────────────────────────

def render_stats():
    if not CORPUS.exists():
        return '<div class="card"><div class="empty">corpus.json ещё не собран</div></div>'
    c = json.loads(CORPUS.read_text())
    st = compute(c)
    st = compute_threads(c, st)
    st = compute_agents(c, st)

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
    lor_pts = " ".join(f"{x*300:.1f},{300-y*300:.1f}" for x, y in lor)
    lorenz = svg(300, 300,
                 f'<line x1="0" y1="300" x2="300" y2="0" stroke="#d2d2d7" stroke-dasharray="4 4"/>'
                 f'<polygon points="0,300 {lor_pts} 300,300" fill="#0071e3" fill-opacity=".12"/>'
                 f'<polyline points="{lor_pts}" fill="none" stroke="#0071e3" stroke-width="2.5"/>'
                 f'<text x="6" y="16" font-size="11" fill="#86868b">доля постов</text>'
                 f'<text x="180" y="292" font-size="11" fill="#86868b">доля агентов</text>')

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

    return f"""
<div class="kpis stat">{kpi}</div>
<div class="charts">
  {card("Пульс доски — постов в час", area_chart(st["hours"], st["per_hour"]),
        f'пик: {st["peak_hour"][0]}:00 — {st["peak_hour"][1]} постов за час. Время красноярское.', wide=True)}
  {card("Треды против ответов",
        area_chart(st["hours"], st["reps_h"], fill="#30d158")
        + area_chart(st["hours"], st["roots_h"], h=90, fill="#ff9f0a"),
        "зелёное — ответы, оранжевое — новые треды. Расхождение вверх у зелёного = доска разговаривает, а не публикуется.", wide=True)}
  {card("Размер треда, ответов", vbars(st["size_hist"]),
        f'медиана {st["size_q"][0]:.0f} · p75 {st["size_q"][1]:.0f} · p90 {st["size_q"][2]:.0f}. '
        f'Без ответа {100*st["dead"]/max(1,st["mature_threads"]):.0f}%, с двумя и больше '
        f'{100*st["alive2"]/max(1,st["mature_threads"]):.0f}%. Исключено {st["young_dropped"]} '
        f'тредов моложе {MATURITY_H} ч — они ещё собирают ответы, и без этой отсечки доля мёртвых завышается.')}
  {card("Задержка первого ответа", vbars(st["lat_hist"]), lat_line)}
  {card("Интервал между постами", vbars(st.get("gap_hist", [])), gap_line +
        ". B=0 у пуассоновского потока, B→1 у пачечного: доска пишет очередями, а не ровно.")}
  {card("Длина превью, символов", vbars(st["len_hist"]),
        f'{100*st["censored"]/st["n"]:.0f}% постов упираются в обрез ленты ровно на {CAP} символах — '
        f'настоящий хвост распределения отсюда не виден, это цензурированная выборка справа.')}
  {card("Кто сколько написал", hbars(st["top_agents"]),
        f'Джини {st["gini"]:.2f} · верхние 10% агентов дают {st["top10pct_share"]:.0f}% постов · '
        f'топ-3 — {st["top3_share"]:.0f}% · агентов ровно с одним постом: {st["one_post_agents"]}')}
  {card("Кривая Лоренца", lorenz, "пунктир — идеальное равенство. Провал вниз = концентрация активности.")}
  {card("Закон Ципфа, log-log", zipf, "прямая линия означала бы степенное распределение — как в естественных сообществах.")}
  {card("Темы", hbars(st["topics"]))}
  {card("Язык заголовков", hbars(st["lang"], colorize=False), "детект по алфавиту заголовка корневого поста.")}
  {card("Рост населения — уникальных агентов", area_chart(st["agents_labels"], st["agents_curve"], fill="#bf5af2"),
        "кумулятивно: сколько разных имён доска увидела к этому часу.", wide=True)}
  {card("Часы активности топ-агентов (Krsk)", heatmap(st["hm_grid"], st["hm_rows"], st["hm_cols"]),
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
Длины считаны по полю <code>preview</code>, обрезанному на ~{CAP} символах →
{100*st["censored"]/st["n"]:.0f}% значений цензурированы справа, среднюю длину поста по ним считать нельзя.<br>
Размер тредов и задержка ответа считаны только по тредам старше {MATURITY_H} ч
({st["mature_threads"]} шт., отброшено {st["young_dropped"]}) — свежий тред ещё не собрал ответы,
и без этой отсечки «мёртвых» тредов получается заметно больше, чем есть.
</div></div>"""

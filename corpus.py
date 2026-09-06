#!/usr/bin/env python3
"""Global board corpus — metadata for every post the activity feed will still show.

The board has no bulk export and no by-author endpoint, so the only way to get a
population sample is to page /v1/activity backwards and keep what it hands over.
This stores metadata only (no bodies): seq, author, topic, thread, timestamp,
preview length, score.

  ./venv/bin/python corpus.py            # catch up to the tip (cheap, tip-gated)
  ./venv/bin/python corpus.py --back 60  # additionally walk 60 pages further back

IMPORTANT about lengths: /v1/activity returns `preview`, truncated at exactly 280
chars (9078 of 10330 posts sit on that value). Anything at the cap is right-censored —
length stats must say so rather than pretend the distribution ends there.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import server  # noqa: E402

CORPUS = Path(__file__).parent / "corpus.json"
PREVIEW_CAP = 280  # exact truncation point of /v1/activity previews


def load():
    if CORPUS.exists():
        return json.loads(CORPUS.read_text())
    return {"posts": {}, "min_seq": 0, "max_seq": 0, "updated": 0}


def row(it):
    prev = it.get("preview") or ""
    return [
        it.get("id") or "",
        it.get("author") or "?",
        it.get("topic") or "—",
        it.get("thread_id"),          # None => root thread
        it.get("created_at") or 0,
        len(prev),
        1 if len(prev) >= PREVIEW_CAP else 0,
        it.get("score") or 0,
        it.get("title") or "",
        None,                          # slot 9: true body length, filled by bodies.py
        it.get("agent_id") or "",      # slot 10: stable identity — names get renamed
    ]


def page_back(posts, start_before, stop_at, max_pages):
    """Walk the feed backwards from start_before, stopping at stop_at or max_pages.

    Returns (added, lowest_seq_seen, hit_stop). A failed request aborts the walk and
    is reported — a silent break here would look identical to 'reached the end'.
    """
    before, added, lowest, hit = start_before, 0, start_before or 10**9, False
    for p in range(max_pages):
        q = f"/v1/activity?limit=30" + (f"&before={before}" if before else "")
        d = server._call("GET", q)
        if d.get("error"):
            print(f"  ! page {p}: {d['error']}", file=sys.stderr)
            return added, lowest, hit, False
        items = d.get("items") or []
        if not items:
            return added, lowest, hit, True
        for it in items:
            s = it.get("seq")
            if s is None:
                continue
            lowest = min(lowest, s)
            if str(s) not in posts:
                posts[str(s)] = row(it)
                added += 1
        if stop_at and min(i["seq"] for i in items) <= stop_at:
            hit = True
            return added, lowest, hit, True
        before = d.get("next_before") or 0
        if not before:
            return added, lowest, hit, True
    return added, lowest, hit, True


def main():
    c = load()
    posts = c["posts"]
    t0 = time.time()

    tipd = server._call("GET", "/v1/activity?limit=1")
    if tipd.get("error"):
        print(f"tip failed: {tipd['error']}", file=sys.stderr)
        return 1
    tip = (tipd.get("items") or [{}])[0].get("seq", 0)

    # 1. catch up: walk back from the tip until we meet what we already have
    if tip > c["max_seq"]:
        add, low, hit, ok = page_back(posts, 0, c["max_seq"], 400)
        print(f"forward catch-up: +{add} (tip {tip}, had {c['max_seq']})")
    else:
        print(f"tip unchanged at {tip}")

    # 2. optional backfill deeper into history
    if "--back" in sys.argv:
        n = int(sys.argv[sys.argv.index("--back") + 1])
        start = c["min_seq"] if c["min_seq"] else 0
        add, low, hit, ok = page_back(posts, start, 0, n)
        print(f"backfill: +{add} pages<={n}, reached seq {low}")
        c["min_seq"] = low if low else c["min_seq"]

    seqs = [int(s) for s in posts]
    c["min_seq"] = min(seqs) if seqs else 0
    c["max_seq"] = max(seqs) if seqs else 0
    c["updated"] = int(time.time())
    c["posts"] = posts
    # atomic: a reader (dashboard, stats) must never see a half-written corpus
    tmp = CORPUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(CORPUS)
    span = c["max_seq"] - c["min_seq"] + 1
    print(f"corpus: {len(posts)} posts, seq {c['min_seq']}..{c['max_seq']} "
          f"({100*len(posts)/span:.1f}% of range), {CORPUS.stat().st_size//1024} KB, "
          f"{time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

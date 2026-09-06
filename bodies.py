#!/usr/bin/env python3
"""Fill in TRUE post lengths, because /v1/activity previews are cut at 280 chars.

The feed's `preview` field truncates, so 88% of the corpus sits on one value and any
length statistic computed from it measures the truncation, not the writing. The thread
endpoint (/v1/posts/{root}) returns full `body` for the root and every reply, so one
pass over the known root threads recovers real lengths for the whole corpus.

Stores the LENGTH only — bodies are not kept, so corpus.json stays ~1.5 MB.

  ./venv/bin/python bodies.py           # fill everything still missing a real length
  ./venv/bin/python bodies.py --limit 200
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import server  # noqa: E402

CORPUS = Path(__file__).parent / "corpus.json"
ID, A, T, TH, TS, PL, TRUNC, SC, TITLE = range(9)
BL = 9  # true body length, appended lazily


def main():
    c = json.loads(CORPUS.read_text())
    posts = c["posts"]
    by_id = {r[ID]: s for s, r in posts.items() if r[ID]}
    roots = [(int(s), r) for s, r in posts.items() if not r[TH]]
    roots.sort(reverse=True)  # newest first: a resumed run keeps recent data freshest

    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 10 ** 9
    done = filled = pages = 0
    t0 = time.time()

    for _, r in roots:
        rid = r[ID]
        if not rid:
            continue
        # skip a thread only when the root AND every known reply already have a length
        kids = [s for s, x in posts.items() if x[TH] == rid]
        if len(r) > BL and all(len(posts[s]) > BL for s in kids):
            continue
        if done >= limit:
            break
        done += 1

        before, guard = 0, 0
        while guard < 40:
            q = f"/v1/posts/{rid}?limit=30" + (f"&before={before}" if before else "")
            d = server._call("GET", q)
            pages += 1
            if d.get("error"):
                print(f"  ! {rid[:8]}: {d['error']}", file=sys.stderr)
                break
            items = []
            if not before and d.get("post"):
                items.append(d["post"])
            items += (d.get("replies") or {}).get("items", [])
            for it in items:
                s = by_id.get(it.get("id"))
                if s is None:
                    continue
                row = posts[s]
                n = len(it.get("body") or it.get("preview") or "")
                while len(row) <= BL:
                    row.append(None)
                row[BL] = n
                filled += 1
            before = (d.get("replies") or {}).get("next_before") or 0
            guard += 1
            if not before:
                break
        if done % 50 == 0:
            print(f"  {done} threads, {filled} lengths, {pages} pages, "
                  f"{time.time()-t0:.0f}s", flush=True)

    # merge, do not overwrite: corpus.py runs from cron every minute and may have
    # added rows since this process loaded the file. Writing our copy wholesale would
    # silently drop them — and the loss looks exactly like "the feed had nothing new".
    fresh = json.loads(CORPUS.read_text())
    for s_, row in fresh["posts"].items():
        if s_ not in posts:
            posts[s_] = row
        elif len(row) > BL and row[BL] is not None and (
                len(posts[s_]) <= BL or posts[s_][BL] is None):
            while len(posts[s_]) <= BL:
                posts[s_].append(None)
            posts[s_][BL] = row[BL]
    fresh["posts"] = posts
    fresh["bodies_updated"] = int(time.time())
    tmp = CORPUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(fresh, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(CORPUS)
    c = fresh
    have = sum(1 for r in posts.values() if len(r) > BL and r[BL] is not None)
    print(f"done: {done} threads, {filled} lengths written, {pages} pages, "
          f"{time.time()-t0:.0f}s · corpus now has real lengths for {have}/{len(posts)} "
          f"({100*have/len(posts):.1f}%)")


if __name__ == "__main__":
    main()

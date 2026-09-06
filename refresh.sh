#!/bin/bash
# Rebuild the dashboard only when the board's global tip has moved.
# Atomic: build to temp, then move — a half-written page is never served.
cd /home/kesha/.claude/mcp-servers/gpb-mcp || exit 1
OUT=/var/www/gpb/board/index.html
TMP=$(mktemp /tmp/gpb-dash.XXXXXX.html)
# corpus first: the stats tab reads corpus.json, so a dashboard built before the
# catch-up would show numbers one cycle stale. Cheap when the tip has not moved.
timeout 90 ./venv/bin/python corpus.py >/dev/null 2>&1
timeout 120 ./venv/bin/python dashboard.py "$TMP" --if-changed >/dev/null 2>&1
rc=$?     # capture BEFORE anything else runs: this rc drives the skip/fail branches

# Publish the measurement fingerprint as a fetchable file. @abel-cain verified every
# claim in #12804 except the corpus numbers, because they lived only on disk (#12996).
# A standard nobody can fetch is a standard nobody can check. Runs regardless of rc:
# the fingerprint describes the corpus, which corpus.py just refreshed.
timeout 30 ./venv/bin/python - <<'PY' >/dev/null 2>&1
import json
c = json.load(open("corpus.json"))
fp = {k: c[k] for k in ("head_seq", "head_utc", "seq_set_sha256",
                        "seq_set_all_sha256", "seq_set_encoding", "gone")}
fp["records_live"] = len(c["posts"]) - len(c["gone"])
fp["records_all"] = len(c["posts"])
fp["min_seq"] = c["min_seq"]
tmp = "/var/www/gpb/board/.fingerprint.tmp"
json.dump(fp, open(tmp, "w"), indent=1)
import os
os.replace(tmp, "/var/www/gpb/board/fingerprint.json")
PY

if [ $rc -eq 2 ]; then rm -f "$TMP"; exit 0; fi          # tip unchanged, nothing to do
if [ $rc -eq 0 ] && [ -s "$TMP" ]; then
  chmod 644 "$TMP" && mv "$TMP" "$OUT"
else
  rm -f "$TMP"; exit 1
fi

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
rc=$?
if [ $rc -eq 2 ]; then rm -f "$TMP"; exit 0; fi          # tip unchanged, nothing to do
if [ $rc -eq 0 ] && [ -s "$TMP" ]; then
  chmod 644 "$TMP" && mv "$TMP" "$OUT"
else
  rm -f "$TMP"; exit 1
fi

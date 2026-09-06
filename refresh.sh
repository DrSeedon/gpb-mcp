#!/bin/bash
# Rebuild the board dashboard into the web root. Atomic: build to temp, then move.
cd /home/kesha/.claude/mcp-servers/gpb-mcp || exit 1
TMP=$(mktemp /tmp/gpb-dash.XXXXXX.html)
if timeout 280 ./venv/bin/python dashboard.py "$TMP" >/dev/null 2>&1 && [ -s "$TMP" ]; then
  chmod 644 "$TMP" && mv "$TMP" /var/www/gpb/board/index.html
else
  rm -f "$TMP"; exit 1
fi

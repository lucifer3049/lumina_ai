#!/usr/bin/env bash
# PostToolUse hook：Claude 改完 backend 的 .py 後自動 ruff format + ruff check --fix
# 只處理 backend/ 底下的 .py；其他一律略過。
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

f=$(python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
tr = d.get("tool_response") or {}
ti = d.get("tool_input") or {}
p = (tr.get("filePath") if isinstance(tr, dict) else None) or ti.get("file_path") or ""
print(p)
' 2>/dev/null)

[ -n "$f" ] || exit 0
case "$f" in
  "$REPO"/backend/*.py) ;;
  *) exit 0 ;;
esac
[ -f "$f" ] || exit 0

cd "$REPO/backend" || exit 0
uv run ruff format "$f" >/dev/null 2>&1
uv run ruff check --fix "$f" >/dev/null 2>&1
exit 0

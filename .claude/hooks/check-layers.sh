#!/usr/bin/env bash
# Stop hook：任務結束時強制檢查分層依賴（CLAUDE.md 架構鐵則 2）
# 違規時以 systemMessage 顯示給使用者，不阻斷（避免無限重試迴圈）。
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO/backend" || exit 0

if out=$(uv run lint-imports 2>&1); then
  exit 0
fi

python3 -c '
import sys, json
out = sys.stdin.read()
broken = [l.strip() for l in out.splitlines() if "BROKEN" in l]
detail = "; ".join(broken) if broken else out.strip().splitlines()[-1] if out.strip() else "未知"
print(json.dumps({
    "systemMessage": "⚠️ import-linter 分層依賴違規（CLAUDE.md 鐵則 2）：" + detail
}, ensure_ascii=False))
' <<< "$out"
exit 0

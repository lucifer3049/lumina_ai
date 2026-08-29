#!/usr/bin/env python3
"""從工作區的改動推出「該跑哪些測試檔」（`make test-changed`）。

存在的理由：全套在 6 核上要 7 分鐘以上（~1800 條 ＋ 37 支 migration 在每個 xdist
worker 各建一次資料庫），而一次開發迴圈動得到的通常只有幾條。每改一行就跑全套的
結果不是比較安全，是根本不跑。

**這是啟發式，不是安全網。** 它靠檔名對應，看不見「A 改了、B 壞了」這種只有型別
系統或執行期才知道的關係。CLAUDE.md 的「任務結束必跑」指的仍是 `make test` ＋
`make smoke`，push 前那一次不能省——本腳本只縮短中間的迴圈。

對應規則（刻意簡單到可以在腦內重跑，選錯比選少更難察覺）：

1. 改到 `backend/tests/**` 的測試檔本身 → 直接收錄。
2. 改到 `backend/<層>/…/<stem>.py` → 收錄檔名含 `<stem>` 的測試檔。
3. `<stem>` 太泛時（`models`、`tasks`、`__init__`…）改用**上一層目錄名**：
   `apps/platform/models.py` 找的是 `platform`，不是 `models`——後者會把五個
   app 的 model 測試全撈進來，而那與「只跑相關的」是相反的效果。
4. 改到 `Makefile`／`.github/workflows/**` → `test_ci_pipeline.py`（那支測試
   沿著 workflow → Makefile 這條鏈檢查階段存在，改這兩者最容易把它弄紅）。
5. 改到 `backend/pyproject.toml` → `test_layer_contracts.py`（分層 contract 住在
   那裡）。

輸出一行一個路徑，**相對於 `backend/`**（pytest 由 `uv --directory backend` 啟動）。
推不出東西時輸出空的，由 Makefile 決定怎麼講。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
TESTS = BACKEND / "tests"
# 只找這三層：e2e 走 make smoke（前置條件不同，見 Makefile 的 smoke 段）。
LAYERS = ("unit", "integration", "api")

# 這些 stem 在本 repo 裡幾乎每個套件都有一份，拿它去比對等於全撈。
GENERIC_STEMS = frozenset(
    {"models", "tasks", "views", "utils", "base", "config", "schemas", "__init__"}
)
GIT_TIMEOUT_S = 10  # 對外呼叫必有 timeout（CLAUDE.md）


def changed_paths(base: str | None) -> list[str]:
    """工作區改動 ＋ 未追蹤檔；給了 base 就改成「與該 ref 的差異」。"""
    if base:
        cmd = ["git", "diff", "--name-only", f"{base}...HEAD"]
    else:
        cmd = ["git", "diff", "--name-only", "HEAD"]
    tracked = _git(cmd)
    untracked = _git(["git", "ls-files", "--others", "--exclude-standard"]) if not base else []
    return [*tracked, *untracked]


def _git(cmd: list[str]) -> list[str]:
    result = subprocess.run(
        cmd, cwd=REPO, capture_output=True, text=True, check=True, timeout=GIT_TIMEOUT_S
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _all_test_files() -> list[Path]:
    return [p for layer in LAYERS for p in sorted((TESTS / layer).glob("test_*.py"))]


def select(paths: list[str]) -> list[Path]:
    candidates = _all_test_files()
    selected: set[Path] = set()

    for raw in paths:
        path = Path(raw)

        if raw.startswith("backend/tests/"):
            absolute = REPO / path
            # 刪掉的測試檔仍會出現在 diff 裡——跑它會讓 pytest 以「找不到路徑」退出，
            # 而那個錯訊看起來像環境壞了。
            if absolute.exists() and absolute.name.startswith("test_"):
                selected.add(absolute)
            continue

        if raw == "Makefile" or raw.startswith(".github/workflows/"):
            selected.add(TESTS / "unit" / "test_ci_pipeline.py")
            continue

        if raw == "backend/pyproject.toml":
            selected.add(TESTS / "unit" / "test_layer_contracts.py")
            continue

        if not raw.startswith("backend/") or path.suffix != ".py":
            continue

        needle = path.stem if path.stem not in GENERIC_STEMS else path.parent.name
        if not needle or needle in GENERIC_STEMS:
            continue
        selected.update(c for c in candidates if needle in c.stem)

    return sorted(p for p in selected if p.exists())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default=None,
        help="改與某個 ref 的差異（例：--base origin/main，整條分支的改動）",
    )
    args = parser.parse_args()

    for path in select(changed_paths(args.base)):
        print(path.relative_to(BACKEND).as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())

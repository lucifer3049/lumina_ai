#!/usr/bin/env python3
"""從工作區的改動推出「該跑哪些測試檔」（`make test-changed`）。

存在的理由：全套在 6 核上要 7 分鐘以上（~1800 條 ＋ 37 支 migration 在每個 xdist
worker 各建一次資料庫），而一次開發迴圈動得到的通常只有幾條。每改一行就跑全套的
結果不是比較安全，是根本不跑。

**這是啟發式，不是安全網。** 它靠檔名對應，看不見「A 改了、B 壞了」這種只有型別
系統或執行期才知道的關係。CLAUDE.md 的「任務結束必跑」指的仍是全套（目前分三層
跑）＋ `make smoke`，push 前那一次不能省——本腳本只縮短中間的迴圈。

**寧可多選，不可漏選**：多跑幾條的代價是幾秒鐘，而漏掉的那條會以「我明明跑過測試」
的形式在 CI 或別人的機器上出現。以下規則的每個「選不到就全選」都是這條取捨的結果。

對應規則（刻意簡單到可以在腦內重跑）：

1. 改到 `backend/tests/<層>/test_*.py` → 直接收錄（層＝unit／integration／api）。
2. 改到 `backend/tests/` 的**共用夾具**（`conftest.py`、`factories/`、`seed.py`）→
   三層全選。這幾支正是「一改就讓最多測試失效」的檔案，選零個是最糟的答案。
   層內的非測試檔（如未來的 `tests/unit/conftest.py`）則選該層。
3. `backend/tests/e2e/**` 一律**不選**：e2e 走 `make smoke`，前置條件不同（見
   Makefile 的 smoke 段），沒有 `make up` / `migrate` / `gen-jwt-keys` 時它整片紅，
   而紅燈的原因與這次的改動無關。
4. 改到 `backend/<層>/…/<stem>.py` → 收錄檔名含 `<stem>` 的測試檔。
5. `<stem>` 太泛時（`models`、`tasks`、`__init__`…）改用**上一層目錄名**：
   `apps/platform/models.py` 找的是 `platform`，不是 `models`——後者會把五個
   app 的 model 測試全撈進來，而那與「只跑相關的」是相反的效果。
6. 改到 `backend/apps/<app>/migrations/*.py` → 該 app 的測試（同規則 5 的 needle）
   ＋ `test_migrations_in_sync.py`。migration 的 stem 是 `0009_add_x`，對不到任何
   測試檔名，照規則 4 走的結果是「schema 改了卻一條測試都沒選」——而 schema 正是
   最容易打壞 integration／RLS 那層的改動。
7. 改到 `Makefile`／`.github/workflows/**` → `test_ci_pipeline.py`（那支測試
   沿著 workflow → Makefile 這條鏈檢查階段存在，改這兩者最容易把它弄紅）。
8. 改到 `backend/pyproject.toml` → `test_layer_contracts.py`（分層 contract 住在
   那裡）。

輸出一行一個路徑，**相對於 `backend/`**（pytest 由 `uv --directory backend` 啟動）。
推不出東西時輸出空的、離開碼 0，由 Makefile 決定怎麼講；**失敗時離開碼非 0 並把
原因寫到 stderr**——「跑不起來」與「沒有相關測試」在呼叫端長得一樣，不分開講的話
前者會被讀成後者，得到一次都沒跑的假綠。
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
# `backend/tests/` 底下不屬於任何一層、而三層都吃得到的東西。
SHARED_TEST_ENTRIES = frozenset({"conftest.py", "seed.py", "factories", "__init__.py"})
MIGRATIONS_DIR = "migrations"
MIGRATION_SYNC_TEST = "test_migrations_in_sync.py"
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
    # git 的 stderr 被 capture 起來了；不轉述的話呼叫端只看得到一個空輸出加一個
    # 裸 traceback，而最常見的失敗（`--base` 指到本機沒有的 ref）光看 traceback
    # 認不出來。
    printable = " ".join(cmd)
    try:
        # S603：argv 是本檔組出來的靜態清單（只有 --base 的值來自呼叫端，且它是
        # git 的位置參數而非可執行檔），不經 shell。
        result = subprocess.run(  # noqa: S603
            cmd, cwd=REPO, capture_output=True, text=True, check=True, timeout=GIT_TIMEOUT_S
        )
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"git 失敗（{printable}）：{(error.stderr or '').strip()}") from error
    except subprocess.TimeoutExpired as error:
        raise SystemExit(f"git 逾時（{printable}，{GIT_TIMEOUT_S}s）") from error
    except OSError as error:
        raise SystemExit(f"git 執行不了（{printable}）：{error}") from error
    return [line for line in result.stdout.splitlines() if line.strip()]


def _all_test_files() -> list[Path]:
    return [p for layer in LAYERS for p in sorted((TESTS / layer).glob("test_*.py"))]


def _from_tests_tree(path: Path, candidates: list[Path]) -> set[Path]:
    """`backend/tests/**` 底下的改動（規則 1–3）。"""
    entry = path.parts[2] if len(path.parts) > 2 else ""
    if not entry or entry in SHARED_TEST_ENTRIES:
        return set(candidates)  # 共用夾具：三層全選
    if entry not in LAYERS:
        return set()  # e2e（或未來的其他非測試目錄）：交給 make smoke
    absolute = REPO / path
    if absolute.name.startswith("test_"):
        # 刪掉的測試檔仍會出現在 diff 裡——跑它會讓 pytest 以「找不到路徑」退出，
        # 而那個錯訊看起來像環境壞了。
        return {absolute} if absolute.exists() else set()
    return {c for c in candidates if c.parent.name == entry}  # 該層的夾具／helper


def _needle_for(path: Path) -> str:
    """要拿去比對測試檔名的字串（規則 5、6）。"""
    if path.parent.name == MIGRATIONS_DIR:
        return path.parent.parent.name  # apps/<app>/migrations/0009_x.py → <app>
    if path.stem in GENERIC_STEMS:
        return path.parent.name
    return path.stem


def select(paths: list[str]) -> list[Path]:
    candidates = _all_test_files()
    selected: set[Path] = set()

    for raw in paths:
        path = Path(raw)

        if raw.startswith("backend/tests/"):
            selected.update(_from_tests_tree(path, candidates))
            continue

        if raw == "Makefile" or raw.startswith(".github/workflows/"):
            selected.add(TESTS / "unit" / "test_ci_pipeline.py")
            continue

        if raw == "backend/pyproject.toml":
            selected.add(TESTS / "unit" / "test_layer_contracts.py")
            continue

        if not raw.startswith("backend/") or path.suffix != ".py":
            continue

        if path.parent.name == MIGRATIONS_DIR:
            selected.add(TESTS / "unit" / MIGRATION_SYNC_TEST)

        needle = _needle_for(path)
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

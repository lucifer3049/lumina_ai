"""驗收：CI 管線涵蓋 12 §6.1 列出的 PR 階段，且不自建第二套基礎設施。

**為什麼要用測試釘 CI 設定**：CI 是唯一會擋下違規的地方，而它自己沒有任何東西擋。
把 `lint-imports` 或某層 pytest 從 workflow 拿掉，所有測試照樣全綠，
分層腐化與測試漏跑就這樣悄悄開始——這正是 13 §1.2 要防的「AI 跨 session 開發的迴歸盲區」。

**檢查方式是沿著 workflow → Makefile 這條鏈走**，不是比對 workflow 裡的字串：
workflow 各階段呼叫的是 make target（指令只定義一次，避免 Makefile 與 CI 兩份漂），
所以本檔先從 workflow 取出被呼叫的 target，再到 Makefile 讀那些 target 的 recipe，
最後才判斷必要指令在不在。把 `lint-imports` 從 Makefile 的 lint 目標拿掉一樣會紅。

另外三條與階段清單無關、但同樣不能少：

1. **不得出現 GitHub Actions 的 `services:`**：基礎設施定義只能有一份
   （`docker/compose.yml`，經 `make up`）。CI 另外宣告一組 PG/Redis/MinIO 就是第二份
   真相，它會漂——症狀是「CI 綠、本機紅」或反過來，兩邊都不能信。
2. **第三方 action 釘 commit SHA**：tag 可以被移動，等於讓外部帳號決定本 repo 的
   CI 跑什麼（供應鏈風險）。
3. **每個 job 有 timeout**：卡住的 job 會一直燒 runner 時間直到平台上限。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = REPO_ROOT / "Makefile"

# 12 §6.1 PR pipeline 的項目。
REQUIRED_COMMANDS = {
    "ruff lint": "ruff check",
    "ruff format": "ruff format --check",
    "mypy": "mypy",
    "import-linter": "lint-imports",
    "unit 測試": "pytest tests/unit",
    "integration 測試": "pytest tests/integration",
    "api 測試": "pytest tests/api",
    "lockfile 檢查": "lock --check",
    # migration 實際套用在真實 PG 上（extension 也在這一步建）。與下方的
    # 漂移檢查是兩件事：漂移檢查是純記憶體比對，不會發現 migration 本身跑不起來。
    "migration 套用": "manage.py migrate",
    "build image": "docker build",
    # ── 前端（12 §6.1「eslint/vue-tsc + vitest」、03 §6.1）──
    "前端 lint": "eslint",
    # 型別檢查與 lint 分開列：eslint 不做型別推導，vue-tsc 才是 TS strict 的守門。
    # 兩者其中一個被拿掉時，另一個照樣全綠。
    "前端型別檢查": "vue-tsc",
    "前端 unit 測試": "vitest run",
    # ── OpenAPI 契約與 codegen（09 §4、03 §3.1）──
    # 三段鏈缺一不可：匯出（後端 schema → openapi.json）、產生（openapi.json →
    # generated client）、比對（產完之後工作區必須是乾淨的）。
    # 只驗最後一段的話，匯出步驟被拿掉時「契約沒變 → 沒有 diff」，CI 照樣綠，
    # 而契約其實早已與 app 脫節。
    "OpenAPI 契約匯出": "export_openapi.py",
    "前端 client 產生": "openapi-typescript",
    "契約/generated 漂移檢查": "git diff --exit-code",
    # 前端相依必須照 lockfile 裝：不帶 --frozen-lockfile 時 pnpm 會就地更新
    # pnpm-lock.yaml，於是 CI 裝到的版本可能與任何人本機裝的都不同。
    "前端 lockfile 檢查": "--frozen-lockfile",
}

# 12 §6.1 的 migration check 由這支測試實作（不需連 DB，故不在 CI 另立步驟）。
# 這裡只確認它還在——否則 CI 少了 model/migration 漂移的守門而不會有任何徵兆。
MIGRATION_DRIFT_TEST = Path(__file__).with_name("test_migrations_in_sync.py")

_SHA_PINNED = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w.-]+)*@[0-9a-f]{40}$")
_MAKE_INVOCATION = re.compile(r"\bmake\s+([a-z][\w-]*)")
# `pnpm --dir frontend run lint` / `pnpm -C frontend gen:api`：取被呼叫的 script 名。
# 旗標與其參數先吃掉，否則 `--dir` 後面的 `frontend` 會被當成 script 名。
_PNPM_SCRIPT = re.compile(r"\bpnpm\s+(?:(?:-[\w-]+|--[\w-]+)(?:[= ]\S+)?\s+)*(?:run\s+)?([\w:-]+)")

FRONTEND_PACKAGE_JSON = REPO_ROOT / "frontend" / "package.json"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    assert WORKFLOW.exists(), f"缺少 CI workflow：{WORKFLOW.relative_to(REPO_ROOT)}"
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    # YAML 1.1 的坑：未加引號的 `on:` 會被解析成布林 True。正規化回字串，
    # 免得每個讀取處都要記得這件事。
    return {("on" if key is True else key): value for key, value in loaded.items()}


def _jobs(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and jobs, "workflow 沒有定義任何 job"
    return jobs


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in _jobs(workflow).values() for step in job.get("steps", [])]


def _strip_shell_comments(text: str) -> str:
    """去掉註解，但保留引號內的 `#`。

    要去掉註解，是因為把某個階段註解掉（`# make test-api`）之後下面的斷言仍會通過——
    而這正是本檔要擋的情況。

    要保留引號內的 `#`，是因為單純 `line.split("#", 1)` 會把 `grep -hE '...## ...'`
    這種 shell 字串腰斬。目前沒有必要指令落在被腰斬的區段裡，但那是巧合而非保證：
    哪天有人寫出 `sh -c 'pytest tests/api  # 補跑'`，該階段就會被判定為不存在，
    而錯誤訊息會說「CI 缺少階段」，指向完全錯誤的方向。

    引號狀態逐行重置（不跨行追蹤）：shell 字串跨行雖然合法但這裡沒有，
    而一個落單的引號若能吃掉後面所有行，漏檢的範圍會大到無法預期。
    """
    stripped: list[str] = []

    for line in text.splitlines():
        quote: str | None = None
        cut = len(line)
        for index, char in enumerate(line):
            if quote is not None:
                if char == quote:
                    quote = None
            elif char in "'\"":
                quote = char
            elif char == "#":
                cut = index
                break
        stripped.append(line[:cut])

    return "\n".join(stripped)


def _workflow_run_text(workflow: dict[str, Any]) -> str:
    return _strip_shell_comments("\n".join(str(step.get("run", "")) for step in _steps(workflow)))


def _invoked_make_targets(workflow: dict[str, Any]) -> set[str]:
    return set(_MAKE_INVOCATION.findall(_workflow_run_text(workflow)))


_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)\s*[:?+]?=\s*(.*)$")
_TARGET = re.compile(r"^([a-z][\w-]*)\s*:(?!=)\s*(.*)$")
_VARIABLE_REFERENCE = re.compile(r"\$\(([A-Z][A-Z0-9_]*)\)")


def _makefile_variables() -> dict[str, str]:
    """Makefile 的變數定義（`NAME := value`）。"""
    variables: dict[str, str] = {}
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("\t"):
            continue
        if match := _ASSIGNMENT.match(line):
            variables[match.group(1)] = match.group(2)
    return variables


def _expand(text: str, variables: dict[str, str], depth: int = 5) -> str:
    """展開 `$(NAME)`。

    不展開的話，`$(PNPM) run lint` 這種寫法會讓本檔的斷言全部落空——指令**確實**
    在 CI 裡跑，只是文字裡看不到 `pnpm`。而失敗訊息會說「CI 缺少階段」，
    指向完全錯誤的方向（實際踩過）。

    深度上限防的是變數互相引用造成的無限展開；到達上限就原樣留著，
    留著的最壞後果是某條斷言誤判為缺少，不會是安靜通過。
    """
    for _ in range(depth):
        expanded = _VARIABLE_REFERENCE.sub(lambda m: variables.get(m.group(1), m.group(0)), text)
        if expanded == text:
            break
        text = expanded
    return text


def _targets_with_prerequisites(targets: set[str]) -> set[str]:
    """把前置目標也算進來（`openapi-check: openapi gen-api`）。

    只看被 workflow 直接呼叫的那個 target 是不夠的：實際跑起來時 make 會先跑
    前置目標，真正執行匯出與產生的正是它們。少了這段，把 `openapi` 從前置清單
    移除會讓 CI 只剩「比對」而不再重新產生——於是永遠沒有 diff，永遠綠燈。
    """
    prerequisites: dict[str, list[str]] = {}
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("\t") or line.startswith(".PHONY"):
            continue
        if match := _TARGET.match(line):
            # recipe 說明（`## …`）不是前置目標
            prerequisites[match.group(1)] = match.group(2).split("##", 1)[0].split()

    resolved = set(targets)
    pending = list(targets)
    while pending:
        for name in prerequisites.get(pending.pop(), []):
            if name in prerequisites and name not in resolved:
                resolved.add(name)
                pending.append(name)
    return resolved


def _makefile_recipes(targets: set[str]) -> str:
    """取出指定 target（含其前置目標）的 recipe 行，並展開變數。"""
    wanted = _targets_with_prerequisites(targets)
    variables = _makefile_variables()
    recipes: list[str] = []
    current: str | None = None

    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("\t"):
            if current in wanted:
                recipes.append(line.strip())
            continue
        match = _TARGET.match(line)
        current = match.group(1) if match else None

    return _strip_shell_comments(_expand("\n".join(recipes), variables))


def _pnpm_scripts(recipes: str) -> str:
    """recipe 呼叫到的 pnpm script 的實際指令（frontend/package.json 的 `scripts`）。

    鏈要多追這一段，理由與「workflow → Makefile」那段完全相同：Makefile 裡寫的是
    `pnpm --dir frontend run lint`，真正跑什麼定義在 package.json。少了這段，
    把 `eslint` 從 package.json 的 lint script 換成 `echo ok` 一樣全綠。
    """
    if not FRONTEND_PACKAGE_JSON.exists():
        return ""

    package = json.loads(FRONTEND_PACKAGE_JSON.read_text(encoding="utf-8"))
    scripts = package.get("scripts", {})
    if not isinstance(scripts, dict):
        return ""

    invoked = set(_PNPM_SCRIPT.findall(recipes))
    # script 之間會互相呼叫（`pnpm run lint` 內含 `pnpm run typecheck`），
    # 但只展開一層就夠：本檔要的是「這些指令有沒有被 CI 跑到」，
    # 而多層展開需要處理循環引用，複雜度不划算。
    nested = {name for value in scripts.values() for name in _PNPM_SCRIPT.findall(str(value))}
    return "\n".join(str(command) for name, command in scripts.items() if name in invoked | nested)


def _effective_commands_of(run_text: str) -> str:
    """一段 `run` 文字實際會執行到的指令。

    沿三段鏈展開：`run` → 它呼叫的 make target 的 recipe → recipe 呼叫的
    pnpm script 的定義。
    """
    recipes = _makefile_recipes(set(_MAKE_INVOCATION.findall(run_text)))
    return "\n".join([run_text, recipes, _strip_shell_comments(_pnpm_scripts(recipes))])


def _effective_commands(workflow: dict[str, Any]) -> str:
    """整份 workflow 實際會執行到的指令文字。"""
    return _effective_commands_of(_workflow_run_text(workflow))


def test_triggers_on_pull_request_and_main(workflow: dict[str, Any]) -> None:
    """PR 必經 CI（12 §6.1 trunk-based）；main 上的 push 也要跑，抓合併後才浮現的衝突。"""
    triggers = workflow.get("on")
    assert isinstance(triggers, dict), "workflow 的觸發條件缺失或格式不符"
    assert "pull_request" in triggers, "CI 未在 pull_request 觸發"
    assert "push" in triggers, "CI 未在 push 觸發"


@pytest.mark.parametrize(("label", "command"), list(REQUIRED_COMMANDS.items()))
def test_required_stage_present(workflow: dict[str, Any], label: str, command: str) -> None:
    """12 §6.1 的 PR 階段逐項存在（沿 workflow → Makefile 追）。"""
    assert command in _effective_commands(workflow), (
        f"CI 缺少階段「{label}」——workflow 呼叫的 make target 之中沒有任何一個會執行 `{command}`"
    )


def test_migration_drift_check_still_exists(workflow: dict[str, Any]) -> None:
    """12 §6.1 的 migration check：CI 跑 unit 層，而漂移檢查就是其中一支測試。

    只斷言「CI 有跑 unit」不夠——那支測試被刪掉時 unit 層照樣全綠。
    """
    assert "pytest tests/unit" in _effective_commands(workflow), "CI 未執行 unit 層"
    assert MIGRATION_DRIFT_TEST.exists(), (
        f"{MIGRATION_DRIFT_TEST.name} 不見了——model/migration 漂移將無人把關（05 §5.6）"
    )


# 會建立 FastAPI app 或跑測試的指令——兩者都會 import 到 `get_token_codec`，
# 而它在缺金鑰時 Fail Fast（services/identity/tokens.py：不自動產生暫時金鑰，
# 否則「忘了掛金鑰」的部署會照樣起得來，症狀是使用者隨機被登出）。
_NEEDS_JWT_KEYS = ("pytest", "export_openapi.py")
_JWT_KEY_TARGET = "gen-jwt-keys"


def test_jobs_that_build_the_app_generate_jwt_keys(workflow: dict[str, Any]) -> None:
    """凡是會建 app 或跑測試的 job，都必須先產生 JWT 金鑰——而且要在那之前。

    **這條是補一個真的踩過的洞**：1A-3 把 JWT 認證接上去之後，CI 的三個 job 同時
    紅了整整三次推送，根因只有一個——workflow 從來沒有 `make gen-jwt-keys`。本機看
    不出來，因為金鑰是第一次開發時產生的、之後一直躺在 `backend/.secrets/`（gitignore
    之內，CI 拿不到）。而 12 §6.1 的階段清單只管「指令在不在」，不管「跑得起來嗎」。

    連 unit 層也需要：契約漂移檢查（test_openapi_export.py）會建 app，那條路徑一路
    走到 dependency 的 import 期讀檔。順序也要驗——步驟放在後面等於沒放。
    """
    for name, job in _jobs(workflow).items():
        steps = job.get("steps", [])
        runs = [_strip_shell_comments(str(step.get("run", ""))) for step in steps]

        needs_at = next(
            (
                index
                for index, run in enumerate(runs)
                if any(marker in _effective_commands_of(run) for marker in _NEEDS_JWT_KEYS)
            ),
            None,
        )
        if needs_at is None:
            continue

        keys_at = next((index for index, run in enumerate(runs) if _JWT_KEY_TARGET in run), None)

        assert keys_at is not None, (
            f"job `{name}` 會建立 app 或跑測試，但沒有 `make {_JWT_KEY_TARGET}`——"
            "金鑰缺檔是 Fail Fast，整個 job 會在 import 期就死"
        )
        assert keys_at < needs_at, (
            f"job `{name}` 的 `make {_JWT_KEY_TARGET}` 排在需要金鑰的步驟之後（"
            f"第 {keys_at + 1} 步 vs 第 {needs_at + 1} 步）"
        )


def test_pnpm_is_invoked_from_inside_the_frontend_directory() -> None:
    """Makefile 不得用 ``pnpm --dir frontend``——必須 ``cd`` 進去再呼叫。

    ``--dir`` 只改變 pnpm 自己的工作目錄，而**版本是在 pnpm 啟動之前由 corepack
    決定的**：corepack 從 cwd 往上找帶 ``packageManager`` 的 package.json，repo 根
    沒有那個檔案，於是它改抓 registry 的 latest。抓到的版本與 frontend 釘的版本不同
    時，pnpm 一啟動就拒跑。

    **這個錯誤只在 pnpm 發新版的那一天出現**：latest 恰好等於釘的版本時 corepack
    猜對了，什麼事都沒有。2026-08-09 pnpm 發 11.21.0，CI 四個 job 之一當場全紅，
    而前一次 CI 全綠——中間沒有任何程式碼改動。這條測試把它釘住，否則下次有人
    「順手」改回 --dir，一樣要等下一次 pnpm 發版才會知道。
    """
    # 去註解：Makefile 的註解裡就寫著「不得用 pnpm --dir」這句說明，直接對原始
    # 文字斷言會被自己的文件釣中。展開變數：定義寫的是 `cd $(FRONTEND) && pnpm`。
    makefile = _strip_shell_comments(
        _expand(MAKEFILE.read_text(encoding="utf-8"), _makefile_variables())
    )

    assert "pnpm --dir" not in makefile, (
        "Makefile 用了 `pnpm --dir`——corepack 會從 repo 根解析版本（那裡沒有 "
        "package.json）而抓 latest，與 frontend 釘的版本不符時 pnpm 拒跑。改成 "
        "`cd frontend && pnpm ...`"
    )
    assert "cd frontend && pnpm" in makefile, "找不到從 frontend 目錄呼叫 pnpm 的定義"


def test_image_runs_as_non_root(workflow: dict[str, Any]) -> None:
    """ADR-007「non-root user」不是靠人記得，要在 CI 驗（image 以 root 跑不會有症狀）。"""
    assert "id -u" in _workflow_run_text(workflow), (
        "CI 未驗證 image 以非 root 執行（應跑 `docker run --rm IMAGE id -u` 並斷言非 0）"
    )


def test_image_is_scanned(workflow: dict[str, Any]) -> None:
    """trivy 掃描（12 §6.1）。"""
    parts = [
        _workflow_run_text(workflow),
        *(str(value) for value in workflow.get("env", {}).values()),
        *(str(step.get("uses", "")) for step in _steps(workflow)),
    ]
    assert "trivy" in "\n".join(parts).lower(), "CI 缺少 image 弱點掃描（trivy）"


def test_infrastructure_comes_from_the_single_compose_file(workflow: dict[str, Any]) -> None:
    """基礎設施只有一份真相：不得用 GitHub Actions 的 `services:` 另建一套。"""
    for name, job in _jobs(workflow).items():
        assert "services" not in job, (
            f"job `{name}` 用 GitHub Actions services 另建了基礎設施——"
            "改用 `make up`（docker/compose.yml），否則版本與設定會與本機漂開"
        )
    assert "up" in _invoked_make_targets(workflow), "CI 未以 `make up` 起基礎設施"
    assert "docker/compose.yml" in MAKEFILE.read_text(encoding="utf-8"), (
        "Makefile 的 compose 來源不是 docker/compose.yml"
    )


def test_third_party_actions_are_pinned_to_commit_sha(workflow: dict[str, Any]) -> None:
    """tag 可被移動；釘 SHA 才能保證跑的是審過的那份程式碼。"""
    unpinned = [
        uses
        for step in _steps(workflow)
        if (uses := str(step.get("uses", "")))
        and not uses.startswith("./")
        and not _SHA_PINNED.match(uses)
    ]
    assert not unpinned, f"以下 action 未釘 commit SHA：{unpinned}"


def test_every_job_has_a_timeout(workflow: dict[str, Any]) -> None:
    """卡住的 job 會燒到平台上限才被砍（CLAUDE.md：所有外部互動必有 timeout）。"""
    missing = [name for name, job in _jobs(workflow).items() if "timeout-minutes" not in job]
    assert not missing, f"以下 job 未設 timeout-minutes：{missing}"

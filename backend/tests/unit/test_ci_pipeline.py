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

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = REPO_ROOT / "Makefile"

# 12 §6.1 PR pipeline 中，本階段（尚無前端與 OpenAPI baseline）適用的項目。
REQUIRED_COMMANDS = {
    "ruff lint": "ruff check",
    "ruff format": "ruff format --check",
    "mypy": "mypy",
    "import-linter": "lint-imports",
    "unit 測試": "pytest tests/unit",
    "integration 測試": "pytest tests/integration",
    "api 測試": "pytest tests/api",
    "migration check": "migrate",
    "build image": "docker build",
}

_SHA_PINNED = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w.-]+)*@[0-9a-f]{40}$")
_MAKE_INVOCATION = re.compile(r"\bmake\s+([a-z][\w-]*)")


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


def _workflow_run_text(workflow: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in _steps(workflow))


def _invoked_make_targets(workflow: dict[str, Any]) -> set[str]:
    return set(_MAKE_INVOCATION.findall(_workflow_run_text(workflow)))


def _makefile_recipes(targets: set[str]) -> str:
    """取出指定 target 的 recipe 行（以 tab 起首的行）。"""
    recipes: list[str] = []
    current: str | None = None

    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("\t"):
            if current in targets:
                recipes.append(line.strip())
            continue
        match = re.match(r"^([a-z][\w-]*)\s*:(?!=)", line)
        current = match.group(1) if match else None

    return "\n".join(recipes)


def _effective_commands(workflow: dict[str, Any]) -> str:
    """CI 實際會執行到的指令文字：workflow 的 run ＋ 它呼叫的 make target 的 recipe。"""
    return _workflow_run_text(workflow) + "\n" + _makefile_recipes(_invoked_make_targets(workflow))


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

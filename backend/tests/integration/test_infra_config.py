"""驗收：設定與 secrets 的處理方式（CLAUDE.md 鐵則 9、10 安全設計）。

spike 階段的密碼是寫死在 compose 裡的（`lumina_spike_pw`，當時標註「非機密」）。
Phase 0 全量之後這個豁免結束——本檔就是那條界線的自動化版本：

- compose 檔內不得再出現明文密碼；一律 `${VAR}`。
- `.env.example` 必須涵蓋 compose 用到的每一個變數，否則新人 clone 後照著
  `.env.example` 複製仍然起不來（Phase 0 DoD：新人 30 分鐘內能跑起環境）。
- `.env` 必須被 gitignore（真值不進版控）。
- 應用端缺變數要**啟動即失敗**，不能悄悄用開發預設值連上正式環境（Fail Fast）。

這幾條沒有自動化就必然腐化：加一個服務、順手貼一個密碼，review 未必看得到。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from config.settings.app_settings import AppSettings

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "docker" / "compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
GITIGNORE = REPO_ROOT / ".gitignore"

# compose 內的變數引用：${VAR} / ${VAR:-default}
_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)")
# 疑似明文機密：key/password/secret/token 後面直接跟值，且不是 ${...}
_LITERAL_SECRET_PATTERN = re.compile(
    r"^\s*[A-Z_]*(PASSWORD|SECRET|TOKEN|ACCESS_KEY|SECRET_KEY)[A-Z_]*\s*:\s*(?!\$\{)(\S+)",
    re.IGNORECASE | re.MULTILINE,
)


def _env_example_keys() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


def test_env_example_covers_every_compose_variable() -> None:
    """compose 引用的變數，`.env.example` 必須全部列出（含註解說明用途）。"""
    referenced = set(_VAR_PATTERN.findall(COMPOSE_FILE.read_text(encoding="utf-8")))
    missing = referenced - _env_example_keys()

    assert not missing, f".env.example 缺少 compose 使用的變數：{sorted(missing)}"


def test_compose_contains_no_literal_secrets() -> None:
    """compose 內不得出現明文密碼／金鑰（鐵則 9）。"""
    matches = _LITERAL_SECRET_PATTERN.findall(COMPOSE_FILE.read_text(encoding="utf-8"))

    assert not matches, f"compose.yml 出現疑似明文機密：{matches}——一律改用 ${{VAR}}"


def test_dotenv_is_gitignored() -> None:
    """真值檔不進版控；`.env.example` 必須例外放行。"""
    rules = [line.strip() for line in GITIGNORE.read_text(encoding="utf-8").splitlines()]

    assert ".env" in rules, ".gitignore 未忽略 .env"
    assert "!.env.example" in rules, ".gitignore 未放行 .env.example（新人照抄的樣板）"


def test_app_settings_fail_fast_when_secret_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺必要環境變數要在建構期爆炸，而不是套一個開發預設值繼續跑。"""
    for key in list(os.environ):
        if key.startswith(("REDIS_", "S3_")):
            monkeypatch.delenv(key, raising=False)

    # `_env_file=None`：斷開 .env，否則測試會讀到開發用的真值而永遠不失敗。
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None)  # type: ignore[call-arg]

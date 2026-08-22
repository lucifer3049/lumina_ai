"""驗收：設定與 secrets 的處理方式（CLAUDE.md 鐵則 9、10 安全設計）。

spike 階段的密碼是寫死在 compose 裡的（`lumina_spike_pw`，當時標註「非機密」）。
Phase 0 全量之後這個豁免結束——本檔就是那條界線的自動化版本：

- compose 檔內不得再出現明文密碼；一律 `${VAR}`。
- `.env.example` 必須涵蓋 compose 用到的每一個變數，否則新人 clone 後照著
  `.env.example` 複製仍然起不來（Phase 0 DoD：新人 30 分鐘內能跑起環境）。
- `.env` 必須被 gitignore（真值不進版控）。
- compose 的 port 一律綁 127.0.0.1（否則四個服務帶著開發密碼對區域網路開放）。
- 應用端缺變數要**啟動即失敗**，不能悄悄用開發預設值連上正式環境（Fail Fast）。

這幾條沒有自動化就必然腐化：加一個服務、順手貼一個密碼，review 未必看得到。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml
from django.conf import settings
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


def test_published_ports_are_bound_to_loopback() -> None:
    """port 一律綁 127.0.0.1（`"16432:5432"` 的簡寫等同 0.0.0.0）。

    未綁的話 PG / PgBouncer / Redis / MinIO 會帶著 `change-me-locally` 這種開發密碼
    對整個區域網路開放——接上公用 wifi 時同網段任何人可讀寫全部租戶資料，
    而本機不會有任何徵兆。用 YAML 解析而非字串比對：`ports:` 之外的清單項
    （healthcheck、command）長得很像，正則會誤判。
    """
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    exposed: list[str] = []

    for name, service in compose.get("services", {}).items():
        for mapping in service.get("ports", []):
            # long syntax（dict）目前未使用；出現時一併要求顯式 host_ip
            if isinstance(mapping, dict):
                if mapping.get("host_ip") != "127.0.0.1":
                    exposed.append(f"{name}: {mapping}")
            elif not str(mapping).startswith("127.0.0.1:"):
                exposed.append(f"{name}: {mapping}")

    assert not exposed, f"以下 port 未綁 127.0.0.1，等同對區域網路開放：{exposed}"


def test_dotenv_is_gitignored() -> None:
    """真值檔不進版控；`.env.example` 必須例外放行。"""
    rules = [line.strip() for line in GITIGNORE.read_text(encoding="utf-8").splitlines()]

    assert ".env" in rules, ".gitignore 未忽略 .env"
    assert "!.env.example" in rules, ".gitignore 未放行 .env.example（新人照抄的樣板）"


def test_server_side_cursors_are_disabled_for_transaction_pooling() -> None:
    """PgBouncer 走 transaction pooling，而 Django 的 server-side cursor 預設**開啟**。

    ``QuerySet.iterator()`` 會 ``DECLARE`` 一個 cursor 再分批 ``FETCH``；transaction
    pooling 下兩者落在不同交易、也就是不同的 server 連線，於是
    ``InvalidCursorName: cursor "_django_curs_..." does not exist``。

    在 Phase 0 就釘住的理由是它**測不出來**：config/settings/test.py 直連
    PostgreSQL、繞過 PgBouncer，所以 ``iterator()`` 在整個測試套件裡永遠正常，
    只有部署環境會炸。而 ``iterator()`` 正是 ETL / 匯出處理大量列的標準寫法
    （Phase 2 必然用到），屆時症狀會出現在離設定很遠的地方。
    """
    pgbouncer_ini = (REPO_ROOT / "docker" / "pgbouncer" / "pgbouncer.ini.tpl").read_text(
        encoding="utf-8"
    )

    assert "pool_mode = transaction" in pgbouncer_ini, (
        "pool_mode 不再是 transaction——本條斷言的前提改變了，請重新評估"
    )
    assert settings.DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] is True, (
        "DISABLE_SERVER_SIDE_CURSORS 未開 → QuerySet.iterator() 在部署環境會失敗"
    )


def test_app_settings_fail_fast_when_secret_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺必要環境變數要在建構期爆炸，而不是套一個開發預設值繼續跑。"""
    for key in list(os.environ):
        if key.startswith(("REDIS_", "S3_")):
            monkeypatch.delenv(key, raising=False)

    # `_env_file=None`：斷開 .env，否則測試會讀到開發用的真值而永遠不失敗。
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None)  # type: ignore[call-arg]


class TestSmtpCredentials:
    """`smtp_password` 是檔頭規則 1（憑證沒有預設值）唯一的例外，這裡釘住它的邊界。

    匿名中繼是合法設定（開發的 Mailpit、內網 MTA），所以不能一律必填；但「有帳號、
    沒密碼」永遠是漏帶——空密碼登入會被伺服器以 535 拒絕，症狀是每一封通知信都進
    DLQ，而 DLQ 只說認證失敗，不會說密碼是空的。
    """

    @staticmethod
    def _build(monkeypatch: pytest.MonkeyPatch, **env: str) -> AppSettings:
        """從環境變數建一份設定（`_env_file=None` 斷開 .env，否則會讀到開發真值）。

        走 `setenv` 而不是建構子參數：這條規則要擋的正是「環境變數漏帶」，用參數
        直接塞值等於繞過被測的那條路。
        """
        for key in list(os.environ):
            if key.startswith("SMTP_"):
                monkeypatch.delenv(key, raising=False)
        required = {"REDIS_PASSWORD": "pw", "S3_ACCESS_KEY": "ak", "S3_SECRET_KEY": "sk"}
        for key, value in (required | env).items():
            monkeypatch.setenv(key, value)
        return AppSettings(_env_file=None)  # type: ignore[call-arg]

    def test_username_without_password_is_rejected_in_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValidationError, match="SMTP_PASSWORD"):
            self._build(monkeypatch, ENVIRONMENT="production", SMTP_USERNAME="mailer")

    def test_anonymous_relay_stays_legal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """完全沒有帳密是明確的選擇（Mailpit、內網 MTA），不是漏帶。"""
        settings = self._build(monkeypatch, ENVIRONMENT="production")

        assert settings.smtp_password.get_secret_value() == ""

    def test_development_is_not_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """開發與 CI 用無認證的 Mailpit；在那裡炸掉只會逼人在 .env 塞假值，
        而假值會蓋掉這條檢查真正想擋的東西（同 `refresh_cookie_secure` 的分界）。"""
        settings = self._build(monkeypatch, ENVIRONMENT="development", SMTP_USERNAME="mailer")

        assert settings.smtp_username == "mailer"

"""非 Django 的應用組態（02 §2：Pydantic Settings 讀環境變數）。

Django settings 只管 ORM/Migration（見 base.py）；Redis、物件儲存這類外部依賴的
連線資訊放這裡，理由是它們同樣要被 Celery worker、CLI 腳本、測試使用，
綁在 Django settings 上會逼那些進程也去 `django.setup()`。

三條規則寫死在型別裡，不靠人記得：

1. **憑證沒有預設值**（`SecretStr` 且必填）：缺就在建構期炸掉。有預設值的密碼
   最危險——設定漏帶時程式照跑，只是連到別的地方。
2. **憑證用 `SecretStr`**：`repr()` 與 log 中顯示為 `**********`，
   避免 secrets 隨錯誤訊息或 structlog 事件外流（鐵則 9）。
3. **連線 URL 由片段組出來**，不另存一份完整 URL：兩份一定會漂，
   而漂掉時症狀是「改了密碼卻還是連得上舊的」。

timeout 的值出自 11 §4.1 全域字典（Redis 500ms、MinIO 30s），
由 tests/integration 對帳，改值會紅燈。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/config/settings/app_settings.py → repo 根目錄
REPO_ROOT = Path(__file__).resolve().parents[3]


class AppSettings(BaseSettings):
    """外部依賴的連線組態；來源優先序：環境變數 > repo 根的 `.env`。"""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        # compose 用的變數（DB_*、POSTGRES_* 等）同在一份 .env，不該讓它們報錯。
        extra="ignore",
    )

    # ── Redis（01 附錄 A；埠位非預設，避免撞本機原生 Redis）──
    redis_host: str = "127.0.0.1"
    redis_port: int = 16379
    redis_db: int = 0
    redis_password: SecretStr
    redis_timeout_seconds: float = 0.5  # 11 §4.1

    # ── 日誌（12 §1.1；設定生效點在 config/logging.py）──
    # 容器一律 json（stdout → Loki）；本機開發可設 console 看得舒服一點。
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # ── 執行環境 ──
    # 只影響「為了本機方便而放寬」的設定（目前僅 refresh cookie 的 Secure 旗標）。
    # 預設是 production：漏設環境變數時要落在**嚴格**的那一邊，反過來的話一次
    # 忘記設定就會讓正式環境用開發設定跑，而且完全沒有症狀。
    environment: Literal["development", "test", "production"] = "production"

    # ── JWT（10 §2.1）──
    # 金鑰以檔案路徑注入而非直接放 .env：PEM 是多行的，塞進 .env 要跳脫，貼壞時
    # 的錯誤訊息（"Could not deserialize key data"）完全看不出是格式問題。
    # 本機用 `make gen-jwt-keys` 產生；正式環境由 Secrets Manager 掛檔案進來。
    jwt_private_key_path: Path = REPO_ROOT / "backend" / ".secrets" / "jwt-es256.key"
    jwt_public_key_path: Path = REPO_ROOT / "backend" / ".secrets" / "jwt-es256.pub"
    # 目前只有一把金鑰，但 token 一律帶 kid（見 services/identity/tokens.py）。
    jwt_active_kid: str = "dev-1"

    # ── 登入防護（10 §2.1）──
    login_max_attempts: int = 5
    login_lockout_seconds: int = 900  # 15 分鐘

    # ── 物件儲存（MinIO，S3 相容）──
    s3_host: str = "127.0.0.1"
    s3_port: int = 19000
    s3_use_tls: bool = False
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    s3_bucket: str = "lumina"
    s3_timeout_seconds: int = 30  # 11 §4.1

    @property
    def redis_url(self) -> SecretStr:
        """`redis://:pw@host:port/db`；密碼經 percent-encoding，特殊字元不會拆壞 URL。"""
        password = quote(self.redis_password.get_secret_value(), safe="")
        return SecretStr(f"redis://:{password}@{self.redis_host}:{self.redis_port}/{self.redis_db}")

    @property
    def refresh_cookie_secure(self) -> bool:
        """`Secure` 只在本機開發關掉。

        `Secure` 的意思是「只在 HTTPS 送出」，而本機跑的是 http://localhost——
        開著的話瀏覽器直接丟掉 cookie，症狀是「登入成功、一重新整理就登出」，
        而 devtools 裡看不到那個 cookie，很難聯想到旗標。

        反過來在正式環境少了它，任何一次降級到 http 的請求都會把 refresh token
        明文送上網路。所以放寬的條件寫在這裡、由測試釘住，不留給呼叫端判斷。
        """
        return self.environment != "development"

    @property
    def s3_endpoint(self) -> str:
        scheme = "https" if self.s3_use_tls else "http"
        return f"{scheme}://{self.s3_host}:{self.s3_port}"


@lru_cache
def get_app_settings() -> AppSettings:
    """單例存取點。測試若要改環境變數，需先 `get_app_settings.cache_clear()`（02 §4.1）。"""
    # 下方的 ignore 是必要的：必填欄位的值來自環境變數／`.env`，靜態檢查看不到來源，
    # 會誤報「缺少具名參數」。缺值時仍會在此拋 ValidationError（Fail Fast 不受影響）。
    return AppSettings()  # type: ignore[call-arg]

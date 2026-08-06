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

    # ── 功能旗標 ──
    # spike 壓測面（api/main.py 的 X-Tenant-Id tenant_middleware ＋ /spike 路由）。
    # 兩者無認證且違反 ADR-002「不接受 client 自報 tenant_id」，因此**預設關閉**：
    # 缺這個旗標時 create_app() 不掛 tenant_middleware、也不掛 spike 路由，
    # 未認證的跨租戶讀取面根本不存在。僅在跑 B 組壓測時顯式設 True。
    # 工作包 1A 接上 JWT 認證後，整段 spike 面與本旗標一併刪除（ADR-002 結案條件）。
    enable_spike_endpoints: bool = False

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
    def s3_endpoint(self) -> str:
        scheme = "https" if self.s3_use_tls else "http"
        return f"{scheme}://{self.s3_host}:{self.s3_port}"


@lru_cache
def get_app_settings() -> AppSettings:
    """單例存取點。測試若要改環境變數，需先 `get_app_settings.cache_clear()`（02 §4.1）。"""
    # 下方的 ignore 是必要的：必填欄位的值來自環境變數／`.env`，靜態檢查看不到來源，
    # 會誤報「缺少具名參數」。缺值時仍會在此拋 ValidationError（Fail Fast 不受影響）。
    return AppSettings()  # type: ignore[call-arg]

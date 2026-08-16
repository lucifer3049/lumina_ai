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

    # ── AI Gateway（06 §4；1C-1）──
    # provider 與模型名稱一律來自設定（鐵則 9）：寫死的話，「本機用 mock、正式用
    # OpenAI」這種再普通不過的需求會變成程式裡的一個分支，而換模型要改碼。
    #
    # 預設 `mock`：真 adapter 屬 1C-5，且**測試永遠不打真 API**（CLAUDE.md）。預設值
    # 落在最安全的一邊——漏設環境變數時得到的是假向量而不是一筆真帳單。
    # 1C-5：五家真 provider。全部提供 OpenAI 相容的 `/v1/embeddings`，因此共用一個
    # adapter（`ai/gateway/providers/openai_compatible.py` 的 VENDORS 表）。
    ai_embedding_provider: Literal["mock", "gemini", "openai", "openrouter", "nvidia", "ollama"] = (
        "mock"
    )
    # **只有一組金鑰**：同一時間只有一家在服務 embedding（同一個 KB 的向量必須來自
    # 同一個模型，否則距離沒有意義），所以設定「正在用的那一家」就夠了。
    # `None` 是合法的——本機 Ollama 沒有金鑰概念；缺金鑰而那家需要時，
    # `build_gateway()` 會在**啟動當下**失敗（Fail Fast，理由同 1A 的 JWT 金鑰）。
    ai_embedding_api_key: SecretStr | None = None
    # 空字串 = 用 VENDORS 表裡的預設位址。留這個覆寫是因為 Ollama 的位址隨部署而異
    # （本機 / 區網 / 容器內），而那不該逼人去改程式碼。
    ai_embedding_base_url: str = ""
    ai_embedding_model: str = "mock-embedding"
    # 維度要與 05 §3.2 的 `halfvec(1536)` 一致。放進設定是因為換模型就會換維度，
    # 而那時 migration 與這裡必須一起改——兩邊對不上時 INSERT 會被 DB 擋下。
    ai_embedding_dimensions: int = 1536
    # 06 §4 的 timeout 分三段（連線 10s / TTFT 30s / 整體 120s）是給串流用的；
    # embedding 是一次性呼叫，只需要整體上限。批次 64 筆的 API 呼叫通常 < 5s。
    ai_embedding_timeout_seconds: float = 30.0

    # ── AI Gateway：串流對話（06 §4；1D-3a）──
    # **與 embedding 是兩組獨立的設定**，即使兩邊常常是同一家。理由是它們的選型依據
    # 完全不同：embedding 綁著整個知識庫的向量（換了就要重算），chat 換一家是換一次
    # 請求。共用一組的話，想換聊天模型就得連帶動到 embedding，而那是重嵌入。
    ai_chat_provider: Literal["mock", "gemini", "openai", "openrouter", "nvidia", "ollama"] = "mock"
    ai_chat_api_key: SecretStr | None = None
    ai_chat_base_url: str = ""
    ai_chat_model: str = "mock-chat"
    # fallback 鏈（06 §4）：primary 在**尚未輸出任何 token** 時失敗才會往下走。
    # 逗號分隔而不是 list：它來自環境變數，而 pydantic 對 list 的環境變數要求 JSON
    # 字面值（`["a","b"]`），在 .env 裡貼那個很容易貼壞，且錯的形狀會讓服務起不來。
    # 預設空字串 = 沒有 fallback：鏈上的每一個模型都要有人確認過它答得出同樣品質的
    # 東西，而那是產品決策，不該由一個預設值代做。
    ai_chat_fallback_models: str = ""
    # 三層逾時，各自擋不同的故障（06 §4）：
    #   connect —— 連不上（DNS、TLS、對方沒開）。
    #   ttft    —— 連上了但第一個字遲遲不來（對方塞車）。這一段還在分水嶺之前，
    #              所以逾時可以安全地換一個模型重來。
    #   total   —— 吐得很慢但一直沒停。沒有它的話，一個壞掉的 provider 可以讓一條
    #              連線與一份 quota 保留量停在那裡好幾個小時，而監控上只看得到
    #              「有一個請求還在跑」。
    # reasoning 模型的 TTFT 天然更長，06 §4 已載明啟用時由 model_config 覆寫（2A）。
    ai_chat_connect_timeout_seconds: float = 10.0
    ai_chat_ttft_timeout_seconds: float = 30.0
    ai_chat_total_timeout_seconds: float = 120.0

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

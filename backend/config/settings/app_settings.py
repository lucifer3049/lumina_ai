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

    # ── 可調參數：檢索與切塊（15 §4.1；1D-5）────────────────────────
    #
    # **「要由使用者決定」的數字全部住在這一段**，不散在 service 的常數裡。這是
    # 2026-08-17 的產品決定（15 §4.1），而它要防的不是「難找」——是同一個概念在兩處
    # 各有一份預設值：1D-5 之前 `top_k=40` 同時寫在 `RetrievalService` 與 `/rag/query`
    # 的簽章上，兩份漂掉時的症狀是「除錯 API 查得到、實際問答查不到」，而兩邊各自
    # 看起來都對。
    #
    # 覆寫順序（解析在 `services/rag/params.py`）：
    #     這裡（env 可蓋）→ 租戶設定（09 §2.6，屬 2C）→ KB 覆寫（`kb.config`）
    #
    # 後台的統一設定畫面屬 2C；它要寫的就是上面第二、三層，不必回頭動這裡。
    # **不屬於這一段的**：安全邊界與保護 DB 的硬上限（`services/rag/params.py` 的
    # `MAX_TOP_K`、`ai/prompts/` 的定界標記）——那些不是使用者該決定的東西。

    # 06 §3.1：vector search 候選數。
    rag_top_k: int = 40
    # 06 §3.1 的 rerank top_n 6~8。Phase 1 沒有 rerank，這是「進 context 幾段」。
    rag_context_chunks: int = 8
    # 06 §3.2：RAG context 的 token 預算。與 chunker 用同一個估算器（`etl/tokens.py`），
    # 兩邊估法不同時這個預算的算術就對不起來。
    rag_context_token_budget: int = 4500
    # 相對門檻：只留下分數 ≥ 第一名 × 這個比例的候選。**預設 0 = 關閉。**
    # 06 §3.1 的絕對門檻 0.3 是 cross-encoder 的尺度，套在餘弦相似度上等於每次都回
    # 「找不到相關內容」；相對門檻不吃尺度，但它仍會砍東西，所以開不開由資料決定。
    # 絕對門檻等 2B 接上 `bge-reranker-v2-m3`（MIT、可自架）之後才有意義。
    rag_min_score_ratio: float = 0.0
    # 檢索時往前帶幾個問題（06 §3.1 的 condense 的免錢版，1D-5）。0 = 只看當前問題。
    rag_query_history_turns: int = 1

    # 08 §3 的切塊參數。**原本住在 `etl/chunk.py` 的 dataclass 預設值**，1D-5 依
    # 15 §4.1 搬過來——留在那裡的話，「統一管理」對切塊這半邊就是假的。
    chunk_target_tokens: int = 512
    chunk_overlap_tokens: int = 64

    # 模型價目表（05 §3.3、2A-1）。格式 `model:prompt/completion;...`，單位
    # USD / 1M tokens（業界報價單位，抄價目表不必換算）。不用 JSON——同
    # `ai_chat_fallback_models` 的理由。解析在 `services/platform/pricing.py`
    # （壞條目只失去那一條，讀取容忍）。
    #
    # **範圍偏離紀錄（2026-08-21 人類核可）**：05 §3.3 的落點是
    # `model_configs.pricing`，但那張表連同 Model 管理模組（04 §5.1）都還不存在；
    # 先住這裡，model_configs 落地時搬儲存位置、`compute_cost` 介面不變。
    # mock 兩個模型 day-1 就有價（數字是隨意起始點）：沒有的話開發環境的 cost
    # 從第一天起全是 None，Analytics 接上時像「成本功能沒做」。
    ai_model_prices: str = "mock-chat:0.15/0.60;mock-embedding:0.02/0"

    # 配額（04 §8.1，2A-2a）。plan 預設值，格式同價目表的 `resource:value;...`；
    # 沒列的資源＝不限制。租戶覆寫住 `tenant.settings["quota"]`（2C 設定畫面那層），
    # 解析在 `services/platform/quota.py`。起始值：tokens_month 的一百萬對齊
    # 09 §1.3 的錯誤範例；其餘是保守猜測，等真實用量數據再調。
    quota_plan_free: str = (
        "tokens_month:1000000;messages_day:200;documents:100;storage_bytes:1073741824;streams:2"
    )
    # chat 開場對 token/月 的預留量（reserve/commit 的 reserve 值）：實際用量要到
    # 生成結束才知道，先按這個數擋線、結束時校正為實際值。太小＝併發下集體衝線，
    # 太大＝月底「還剩一點額度」的回合被過度擋下。
    quota_token_reserve_estimate: int = 2000

    # 公平佇列（08 §6，2A-2b）：每租戶在 etl／embedding 佇列上的並發上限，與被
    # 讓位的任務多久後回來再試。上限決定公平的粒度（愈小愈公平、單租戶吞吐愈低），
    # 延遲太短會變成空轉輪詢、太長會拉長大批上傳的完成時間。
    etl_max_concurrent_per_tenant: int = 2
    etl_fairness_requeue_seconds: int = 15
    # 分區保留期（05 §7，2A-4）。格式同上：`表名:月數;...`，**沒列的表不動**
    # ——`conversation_message` 的保留期是「依租戶方案」，那個機制還不存在，
    # 而「沒有政策」的正確行為是不動，不是拿某個猜的預設值刪別人的資料。
    # audit 取 3–7 年的**上限**（84 個月）：稽核少留是法遵風險，多留只是空間。
    partition_retention_months: str = "platform_usagelog:13;platform_auditlog:84"
    # 到期分區預設**只從父表摘下（DETACH）**，不刪除：查詢不再掃到它、空間仍在、
    # 錯了可以 ATTACH 回去。這是整套維運任務裡唯一不可逆的一步，因此真的刪除
    # 要在這裡明示開啟（05 §5.2 寫的是 DETACH + DROP，只做前半是 2A-4 的決定）。
    partition_drop_after_detach: bool = False

    # 停滯門檻（補償掃描）：uploaded/chunked 停超過這個秒數視為訊息遺失、補送。
    # 太短會把正常處理中的文件再送一次（冪等擋得住重算、擋不住浪費），太長則
    # 使用者看著「上傳完沒下文」乾等。
    etl_stuck_after_seconds: int = 600

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

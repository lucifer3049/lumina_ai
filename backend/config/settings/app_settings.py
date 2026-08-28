"""非 Django 的應用組態（02 §2：Pydantic Settings 讀環境變數）。

Django settings 只管 ORM/Migration（見 base.py）；Redis、物件儲存這類外部依賴的
連線資訊放這裡，理由是它們同樣要被 Celery worker、CLI 腳本、測試使用，
綁在 Django settings 上會逼那些進程也去 `django.setup()`。

三條規則寫死在型別裡，不靠人記得：

1. **憑證沒有預設值**（`SecretStr` 且必填）：缺就在建構期炸掉。有預設值的密碼
   最危險——設定漏帶時程式照跑，只是連到別的地方。**唯一的例外是 `smtp_password`**
   （匿名中繼是合法設定），而例外的邊界由 `_reject_half_configured_smtp_auth`
   守住——理由寫在那兩處，別再開第二個。
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

from pydantic import SecretStr, model_validator
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
    # 剛被換掉的 refresh token 還能再用幾秒（`AuthService.refresh`）。
    #
    # 不是為了寬鬆，是為了**分辨兩件長得一樣的事**：多分頁同時喚醒會讓同一張
    # refresh 在同一瞬間出現兩次，那是本人；攻擊者的重放則來自別的時間點。沒有這個
    # 窗口的話，前者會被當成後者處理——整個家族撤銷，使用者莫名其妙被登出。
    #
    # 值取秒級而不是分鐘級：窗口內的重放偵測是關掉的，而正常的併發換發只差幾十毫秒。
    # 設 0 = 完全關掉窗口（嚴格輪換），代價是多分頁的誤殺會回來。
    refresh_rotation_grace_seconds: int = 10

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

    # ── Rerank（06 §3.1 的 cross-encoder，2B-3）────────────────────
    #
    # **與 embedding／chat 是第三組獨立設定**（同 1D-3a 的理由）：三者的選型依據完全
    # 不同，而共用一組會讓「換 rerank」變成「連 embedding 一起換」——那是重嵌入。
    #
    # 預設 `mock`：漏設環境變數時要得到的是假分數，而不是一筆真帳單或一個起不來的
    # 服務（同 1C-1）。**2B-4 之後兩個真 adapter 都在**（`ai/gateway/providers/rerank.py`）：
    # `tei` 是自架的主線（`make tei-up` 起容器，模型 `BAAI/bge-reranker-v2-m3`，位址預設
    # `http://127.0.0.1:8080`），`jina` 是雲端第二家（需 `AI_RERANK_API_KEY`）——
    # **預設仍然不動**，接上真服務是一個要用手做的決定。
    ai_rerank_provider: Literal["mock", "tei", "jina"] = "mock"
    ai_rerank_api_key: SecretStr | None = None
    ai_rerank_base_url: str = ""
    ai_rerank_model: str = "mock-rerank"
    # 11 §4：rerank 逾 1.2s 直接跳過（降級鏈）。**不重試**——重試一次就是 2.4s，而
    # 使用者等的是那個，不是更好的排序（見 `AIGateway.rerank`）。
    ai_rerank_timeout_seconds: float = 1.2

    # 06 §3.1：vector search 候選數。
    rag_top_k: int = 40
    # 06 §3.1：FTS（pgroonga）候選數。與 `rag_top_k` 分開是因為兩路的成本不同——
    # 向量那路每次要先算 embedding（真的錢），字面那路只是一次 DB 查詢。
    rag_fts_top_k: int = 40
    # 06 §3.1 的 RRF k=60。「名次差距要壓多平」的旋鈕：越大越看重「有多少路都提到
    # 它」，越小越信任各路自己的排序。
    rag_rrf_k: int = 60
    # 06 §3.1 的「RRF → 24」：融合後留幾筆進下一關。2B-4 的 rerank 吃的就是它，
    # 而 cross-encoder 的成本與這個數字成正比（11 §4 的 rerank < 800ms）。
    rag_hybrid_candidates: int = 24
    # 06 §3.1 的絕對門檻。**預設 0＝關閉，而且 0.3 這個值已被資料推翻**（2B-5 的第四
    # 次評測，2026-08-27）：
    #
    # | 題組 | 正解 p05／p50 | 非正解 p95 | 0.3 砍掉的正解 | 整題全砍 |
    # |------|---------------|------------|----------------|----------|
    # | 手寫 24 題 | 0.0023／0.2429 | 0.1824 | **56%** | **14/24 題** |
    # | DRCD 120 題 | 0.9294／0.9973 | 0.5657 | 0% | 0 題 |
    #
    # 手寫題上**兩群分數重疊**（正解 p05 < 非正解 p95），代表不存在「砍得掉錯的、又
    # 留得住對的」那個數字——連 0.05 都會讓 4 題失去全部正解。DRCD 上無害是因為它是
    # 抽取式 QA：問句由段落本身產生，cross-encoder 給正解近乎滿分，那量不到真實問句
    # 的樣子，而手寫題組存在的全部理由就是這個（2B-0）。
    #
    # 另有一條與尺度有關的理由仍然成立：降級跳過 rerank 之後手上是 RRF 的融合分數
    # （第一名 1/61 ≈ 0.016），套任何絕對門檻都會把候選全砍光。因此它只在 rerank
    # **真的跑完**時才套用，強制位置在 `services/rag/retrieval.py`。
    #
    # 要擋「知識庫無相關內容」請用不吃尺度的相對門檻（`rag_min_score_ratio`）。
    rag_rerank_threshold: float = 0.0

    # 檢索模式（2B-2）。**預設 `vector`，而 06 §3.1 的設計是 hybrid**——這個偏離有
    # 數據支撐，2026-08-23 由使用者裁決：
    #
    # | 策略 | 手寫 24 題 recall@1／mrr | DRCD 120 題 |
    # |------|--------------------------|-------------|
    # | 純向量 | **0.4375** / 0.6046 | **0.9417** / 0.9653 |
    # | hybrid（`&@*` 整句） | 0.3958 / 0.5650 | 0.9333 / 0.9628 |
    # | hybrid（識別符才發言，2B-2b） | 0.4167 / **0.6209** | 0.9250 / 0.9544 |
    #
    # 三種 FTS 策略都沒讓 hybrid 整體勝出。當時的判斷是**問題不在 RRF 也不在 pgroonga，
    # 而在還沒有 rerank**：06 §3.1 的管線是 `RRF → rerank`，cross-encoder 的職責正是把
    # 融合後的候選重新打分，FTS 投的雜訊票本來就該由它修正。
    #
    # **2B-4 接上真的 reranker（本機 TEI + `bge-reranker-v2-m3`）之後重量，答案是：
    # 贏的是 rerank，不是 hybrid。**
    #
    # | 模式 | 手寫 24 題 recall@1／mrr | DRCD 120 題 |
    # |------|--------------------------|-------------|
    # | `vector`（baseline） | 0.4375 / 0.6046 | 0.9417 / 0.9653 |
    # | `hybrid` | 0.4167 / 0.6209 | 0.9250 / 0.9544 |
    # | `vector+rerank` | **0.7917 / 0.8941** | **0.9917 / 0.9944** |
    # | `hybrid+rerank` | **0.7917 / 0.8941** | **0.9917 / 0.9944** |
    #
    # 後兩列不是抄錯：144 題**逐題的正解名次完全相同**。FTS 確實有換掉候選（手寫 5/24、
    # DRCD 8/120 題的 24 段候選集合不同），只是換進來的那幾段從來沒有擠掉正解——
    # cross-encoder 把它們打回去了。也就是說在這兩份題組上，**hybrid 的邊際貢獻是零，
    # 而不是負的**（那是 2B-2 沒有裁判時的情況）。
    #
    # **hybrid 那一路不進預設**：同樣的分數下，多一路 FTS 是多一次 DB 查詢與多一段
    # 融合。程式與測試全部留著——邊際貢獻為零是「這兩份題組上」的結論，而識別符密集的
    # 語料（產品型號、錯誤碼）正是 FTS 該贏的地方，KB 層的 `retrieval_mode` 覆寫開得起。
    #
    # **rerank 那一路進預設**（2026-08-27 使用者裁決，2B-5）：0.4375 → 0.7917 不是可以
    # 留給「記得手動打開」的差距。2B-4 當時不敢改的理由是「漏設 provider 的人會拿字元
    # 重疊比例（`MockRerankProvider`）當 cross-encoder 用，而那比不 rerank 更糟」——
    # 那個風險由下面的 `_reject_mock_rerank_in_production` 擋掉，而不是用一個永遠沒有
    # 人打開的預設值擋。TEI 沒起來時走既有的降級鏈（跳過 rerank，回到 baseline 的品質），
    # 且 2B-5 之後那件事在 `rag_trace` 的 `degraded` 與 `/rag/query` 的回應裡看得見。
    rag_retrieval_mode: Literal["vector", "vector+rerank", "hybrid", "hybrid+rerank"] = (
        "vector+rerank"
    )
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
    # 「處理中」（parsing／cleaned／embedding）多久沒動靜就允許使用者自己重跑。
    #
    # 與上面那個門檻分開，而且大一個數量級：補償掃描是**系統**判斷「訊息掉了、我再
    # 送一次」，冪等保證重送不會弄壞資料；這個是**使用者**判斷「它卡死了、我重來」，
    # 而重跑會建立第二個寫同一份文件的 job。訊息還在飛的時候放行，兩個 job 的 chunk
    # 會互相清掉——結果是內容隨機少一半，兩邊都不報錯。所以寧可讓使用者多等一小時。
    etl_in_progress_stale_seconds: int = 3600
    # ── 軟刪除的保留窗（05 §5.4；二次架構審計 P0-2）────────────
    #
    # 軟刪除的承諾一直是「30 天後由清理 job 硬刪」（`repositories/knowledge.py`、
    # `conversations.py`、`knowledge_bases.py` 三處 docstring 都這麼寫），而那個 job
    # 到 2B 為止都不存在——KB／文件／對話刪掉之後，chunk、向量、物件、訊息全部留著，
    # 而且只增不減。這裡是它的參數。
    #
    # 30 天是「使用者可能後悔」的窗（05 §5.4 的原文）。**調小要小心**：這是整套維運
    # 任務裡第二個不可逆的動作（第一個是分區 DROP），設成 0 等於刪除即硬刪，那時
    # 「刪錯了」沒有任何救回的辦法。
    retention_purge_after_days: int = 30
    # 每個租戶每輪最多處理幾份文件／幾個對話。分批的理由與 `AddIndexConcurrently`
    # 同一個：一個累積了半年的租戶會讓單一交易刪掉數十萬列 chunk 與向量，那條交易
    # 期間 HNSW 索引在抖、其他查詢在等。沒清完的下一輪繼續——job 每天都跑。
    retention_purge_batch_size: int = 500

    # ── 背景生成的行程級上限（11 §2；二次架構審計 F-04）────────
    #
    # 每租戶的 `streams` 額度（預設 2）是**公平性**機制，不是容量機制：租戶數不設限，
    # 所以 N 個租戶 × 2 條是無界的。`api/background.py` 的 `spawn()` 之前無條件
    # `create_task`，一個行程能同時扛幾條生成因此沒有任何答案——症狀不是被擋下，
    # 是全部一起變慢（每條都吃一個 LLM 連線、一份 context、一條 SSE 緩衝），而
    # TTFT 在那個點之後失去意義。
    #
    # 64 是起始值（11 §2 的 p95 目標之下，單行程同時處理數十條串流是可支撐的量級），
    # **待壓測校正**——文件值是起始點，調整要引用數據（CLAUDE.md 開發流程）。
    # 設成 0 或負數視為不設限（回到 2B 之前的行為，給緊急情況一條退路）。
    api_max_concurrent_generations: int = 64
    # 額滿時 429 的 `Retry-After`（秒）。取一個「大於典型生成時間的一小段」而不是
    # 幾分鐘：塞住的原因是瞬時併發，而一條生成的牆鐘上限是 120 秒（`ai_chat_total_
    # timeout_seconds`），等太久等於把可重試的請求變成放棄。
    api_busy_retry_after_seconds: int = 5

    # ── HTTP 頻率限制（09 §1.3、10 §2.1；二次架構審計 F-11＋L3）────
    #
    # 這一層擋的不是配額：配額問「這個租戶這一期還有多少額度」（要先認證），
    # 這裡問「這個來源這一分鐘打了幾次」（必須在認證之前，否則登入端點沒有保護）。
    #
    # **總開關預設開**。關掉的正當理由只有一個：某個環境沒有 Redis，而那時整套
    # 限流本來就會 fail open（見 middleware），關掉只是少寫幾筆 log。
    rate_limit_enabled: bool = True
    # 一般端點：正常使用者開一個聊天頁就會打十幾次 API，300/分鐘留了很寬的餘裕。
    # **待壓測校正**——文件值是起始點（CLAUDE.md 開發流程）。≤0 = 該桶不限流。
    rate_limit_per_minute: int = 300
    # 認證端點（`/api/v1/auth/*`）：那裡的每一次請求都在猜密碼或換 token。
    # 20/分鐘擋得住暴力破解與 L3 的鎖定型 DoS，而正常人一分鐘不會登入 20 次。
    rate_limit_auth_per_minute: int = 20
    # 429 的 `Retry-After`（秒）。取「到下一個時窗」的量級——固定時窗之下，
    # 被擋的人最多等 60 秒就會拿到新額度。
    rate_limit_retry_after_seconds: int = 60
    # **是否採信 `X-Forwarded-For`。預設 False，而且這個預設是安全相關的**：
    # 那個標頭是 client 送的，直接採信等於讓任何人自報假 IP——每個請求換一個，
    # 限流就完全失效，且它會**安靜地**失效（計數器照樣在動，只是每個 key 都是 1）。
    # 只有在確定有一個我們控制的反向代理會覆寫它時才開（Phase 4）。
    rate_limit_trust_proxy_headers: bool = False

    # 終局事件送出之後，SSE 緩衝區還留多久（二次架構審計 L1）。
    #
    # 生成期間的 5 分鐘（`core/streams.py` 的 `BUFFER_TTL_SECONDS`）是為了「長回答
    # 不能中途過期」；收尾之後緩衝區只剩一個用途——**client 在收到 `done` 之前就
    # 斷線了，重連回來補讀最後幾個事件**，而那只會發生在幾秒內。
    #
    # 60 秒是「夠一次重連 + 一點網路抖動」。**調成 0 等於關掉續傳**：斷線的 client
    # 回來會拿到 409 `RESUME_EXPIRED`，那是把成本問題換成使用者看得見的錯誤。
    stream_settled_ttl_seconds: int = 60

    # 訊息卡在 `streaming` 多久算生成已死（補償掃描標成 interrupted）。
    # 生成本身有 120 秒的牆鐘上限（06 §4），超過這個門檻代表產生它的行程已經不在了
    # ——OOM、被 kill -9、機器沒了。優雅關機有自己的收尾路徑（chat.py 的 shield）。
    stream_stuck_after_seconds: int = 600

    # ── 通知（04 §8.5，2A-5）────────────────────────────────
    # email 通道的總開關。關掉時**只失去 email**，站內收件匣照常——沒有設定 SMTP
    # 的環境（CI）不該連通知本身一起失去。
    notification_email_enabled: bool = True
    # SMTP 連線。開發環境是 compose 裡的 Mailpit（收信不外送），正式環境換值即可
    # ——鐵則 9：不 hardcode host／port／寄件人。
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    # **檔頭規則 1（憑證沒有預設值）的刻意例外**，理由與代價都寫在這裡：
    # 大量的 SMTP 中繼站根本不做認證（開發環境的 Mailpit、正式環境常見的內網 MTA），
    # 把它設成必填等於逼每個環境為一個用不到的東西塞一個假值——而假值會讓「這裡到底
    # 有沒有在認證」變得看不出來。
    # 例外的邊界由下面的 `_reject_half_configured_smtp_auth` 守著：沒有帳號就是匿名
    # 中繼（合法），有帳號卻沒有密碼則是**設定漏帶**（規則 1 描述的那種病），要在建構期
    # 就炸掉，不能讓它在正式環境跑成「每封信都 535 認證失敗」。
    smtp_password: SecretStr = SecretStr("")
    smtp_use_tls: bool = False
    # 所有對外呼叫都要有 timeout。收信端不回應時，沒有它的那條 worker 執行緒會
    # 一直掛著，而症狀是「寄信的 worker 慢慢變成沒有人」。
    smtp_timeout_seconds: float = 10.0
    notification_email_from: str = "lumina@example.com"
    # 「文件已完成」的收合視窗（分鐘）：同一個 KB、同一個視窗內的 ready 合成一則。
    # 太大會把兩小時前的上傳算成同一批，太小等於沒有收合（一次上傳 50 份就是 50 則）。
    notification_collapse_window_minutes: int = 10
    # quota 告警的門檻（百分比，`;` 分隔）。80 是「該注意了」、100 是「已經被擋住」
    # ——兩件不同的事，因此是兩則通知（04 §8.5）。
    notification_quota_thresholds: str = "80;100"

    @model_validator(mode="after")
    def _reject_half_configured_smtp_auth(self) -> AppSettings:
        """有 SMTP 帳號就必須有密碼（正式環境）。

        只擋「一半」的設定，不擋「完全沒有」：後者是明確選擇匿名中繼，前者則永遠是
        漏帶——空密碼登入會被伺服器以 535 拒絕，而症狀是**每一封通知信都進 DLQ**，
        而 DLQ 裡的錯誤只說認證失敗，不會說密碼是空的。

        限定 production 的理由與 `refresh_cookie_secure` 相同：開發與 CI 用的是無認證
        的 Mailpit，讓它們為此炸掉只會逼人在 `.env` 裡塞假值，而假值會蓋掉這條檢查
        真正想擋的東西。`environment` 預設就是 production，漏設環境變數時落在嚴格那邊。
        """
        if (
            self.environment == "production"
            and self.notification_email_enabled
            and self.smtp_username
            and not self.smtp_password.get_secret_value()
        ):
            raise ValueError(
                "SMTP_USERNAME 有值但 SMTP_PASSWORD 是空的——正式環境不接受半套的認證設定"
            )
        return self

    @model_validator(mode="after")
    def _reject_mock_rerank_in_production(self) -> AppSettings:
        """正式環境不准用 `MockRerankProvider` 當 cross-encoder（2B-5）。

        `MockRerankProvider` 打的是**字元重疊比例**。它排出來的順序看起來完全合理
        （相關的段落確實傾向共用字），分數也在 0~1，`rag_trace` 裡 `applied=True`
        ——沒有任何一個地方會顯示 rerank 其實沒在工作，而它比不 rerank 更糟：真的
        cross-encoder 修正的正是「字面像但語意無關」那一類候選，mock 反而偏袒它們。

        這條檢查存在的理由是 `rag_retrieval_mode` 現在**預設就含 rerank**
        （2026-08-27 裁決）。預設值把「要不要 rerank」從一個手動決定變成自動的，
        於是「provider 漏設」從一個顯眼的疏忽變成一個安靜的預設——Fail Fast 是把
        它換回顯眼。

        限定 production 的理由同 `_reject_half_configured_smtp_auth`：開發與 CI 就是
        要能在沒有 GPU 的機器上跑完整條 RAG 路徑，讓它們為此炸掉只會逼人把模式改回
        `vector`，而那會讓測試涵蓋的路徑與正式環境的不同。`environment` 預設就是
        production，漏設環境變數時落在嚴格那邊。
        """
        if (
            self.environment == "production"
            and self.rag_retrieval_mode.endswith("+rerank")
            and self.ai_rerank_provider == "mock"
        ):
            raise ValueError(
                "RAG_RETRIEVAL_MODE 含 rerank 但 AI_RERANK_PROVIDER=mock"
                "——mock 打的是字元重疊比例，不是 cross-encoder；"
                "正式環境請設 tei 或 jina，或把模式改成不含 rerank 的"
            )
        return self

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

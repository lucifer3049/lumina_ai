# Contract: 內部介面（Gateway 能力、ETL stage、物件儲存）

這些不是 HTTP 契約，但它們是**跨層的介面**，改動時的破壞範圍與 API 相同——寫在這裡是為了
讓 tasks 有可對照的形狀，也讓 import-linter 的 9 條 contract 在設計階段就被檢查過一次。

---

## 1. AI Gateway 的第四種能力：`VisionProvider`

**位置**：`backend/ai/gateway/providers/__init__.py` 加一個 Protocol；實作放
`backend/ai/gateway/providers/vision.py`；工廠在 `backend/ai/gateway/__init__.py`。

### 1.1 Protocol

```
class VisionProvider(Protocol):
    def describe_image(
        self,
        image: bytes,
        *,
        media_type: str,
        prompt: str,
        model: str,
        timeout_seconds: float,
    ) -> ProviderVision: ...
```

```
ProviderVision（frozen dataclass）
├── text: str                 # 描述文字
├── model: str                # provider 回報的模型（不是我們要求的那個）
├── prompt_tokens: int
└── completion_tokens: int
```

**為什麼不是 `AsyncGenerator`**：caption 不需要串流。它跑在 worker 的 ETL 路徑上，沒有人
在等第一個 token。

**回報的 `model` 要用 provider 回的那個**：同 embedding 的既有做法（`verify_provider` 印的
是「回報模型」而不是要求的模型）——兩者不一致時，那是唯一看得出來的地方。

### 1.2 Gateway 入口

```
class AIGateway:
    def describe_image(
        self, image: bytes, *, media_type: str, prompt: str, model: str,
        timeout_seconds: float | None = None,
    ) -> VisionResult: ...
```

與既有的 `embed()`／`rerank()` 同構（同步、單次、帶逾時）。`build_gateway()` 多解析一個
optional provider，形狀比照既有三個。

### 1.3 設定（第四組獨立設定）

```
ai_vision_provider: Literal["mock", "vllm", "openai", "gemini", "openrouter"] = "mock"
ai_vision_api_key:  SecretStr | None = None
ai_vision_base_url: str = ""
ai_vision_model:    str = "mock-vision"
ai_vision_timeout_seconds: float = 60.0
```

**為什麼是第四組而不是複用 chat 那一組**：`app_settings.py` 對既有三組獨立設定的註解已經
寫明理由是「選型依據不同」。caption 的選型依據（能看圖、跑得動、便宜、慢一點沒關係）與
對話（TTFT、串流、工具呼叫、fallback 鏈）完全不同。這是照著既有理由走，不是新形狀。

**預設 `mock`**：同既有三組——漏設環境變數時要得到假東西，不是一筆真帳單（1C-1 起的慣例）。

**`vllm` 要在 `VENDORS` 新增一列**（`http://127.0.0.1:${VLLM_PORT}/v1`、
`requires_api_key=False`、`supports_dimensions=False`），與既有的 `tei` 那列同形——依既有
規則「**加一家廠商是加一列，不是加一個實作**」（W1 為 `tei` 做過同一件事）。

**vLLM 容器要用才起**：它按 `gpu_memory_utilization` 預先佔住 VRAM 且不會閒置卸載，而卡上
只剩 3.0 GB。因此放 `gpu` profile、配 `make vllm-up`／`-down`／`-logs`，形狀比照兩個 TEI
容器（research R-04）。

### 1.4 `MockVisionProvider`

決定性輸出（比照 `MockEmbeddingProvider` 的 SHA-256 做法）：同一張圖永遠得到同一段描述。
**所有自動測試一律用它**（憲章原則 IV：LLM 測試禁止呼叫真實 API）。

### 1.5 `make verify-provider CAPABILITY=vision`

`scripts/verify_provider.py` 的 `--capability` 加第四個選項。驗三件事並印出來：

1. **回報模型**與**單張耗時**（research R-04 的待驗項 ④ 靠它量）
2. 描述文字**非空**且不是把提示詞照抄回來
3. **中文圖表的描述堪不堪用**（待驗項 ⑤）——這一項只印出來給人看，不做自動判定

同既有三種：只印模型／用量／耗時，**不印金鑰、不印影像內容**。

---

## 2. caption 的 prompt

| 項目 | 值 |
|------|-----|
| 模板 key | `image_caption` |
| 落地方式 | seed migration，形狀比照 `apps/ai/migrations/0004_seed_rag_prompt.py` |
| 變數 | `page`（頁碼，可能為 null）、`heading_path`（所在章節）、`nearby_text`（前後文） |
| `model_hint` | 記下這份模板是為哪個模型寫的 |

**為什麼一定要版本化**：鐵則 5 的後半（禁止散落 Python string），但更實際的理由是——
caption 的提示詞會反覆調整，而**調整的效果只能靠比較兩次產出來判斷**；沒有版本號就沒得比。

---

## 3. ETL 的新 stage：`image`

### 3.1 位置

```
extract ──► image ──► clean ──► chunk ──► （另一條佇列）embed
```

**排在 `clean` 之前，這是必要的**：`clean` 會把處理後的 `ExtractedDoc` 落成中間產物
（`{storage_key}/v{doc_version}/cleaned.json`），而斷點續跑正是從那份產物讀回來的。圖片
產生的 caption block 若沒有在它落地**之前**併進 blocks，續跑會拿到一份沒有圖片內容的產物
——**而且它看起來完全正常**。

### 3.2 這個 stage 做什麼

輸入：`extract` 產出的 `ExtractedDoc`（含 `images`）。

1. 逐張把 `ExtractedImage.content` 寫進物件儲存
   （`{storage_key}/v{doc_version}/images/{seq}.{ext}`）
2. 逐張跑 OCR——**每張各自一次 `run_isolated`**，各自帶逾時（research R-02）
3. 逐張經 `AIGateway.describe_image()` 產生描述，並記一筆 `UsageLog(category="vision")`
4. 把 OCR 文字 ＋ 描述 ＋ 前後文組成 `Block(type=CAPTION)`，依 `ExtractedImage.order`
   插回 blocks 序列
5. 統計寫進 `EtlJob.stats`（形狀見 [data-model.md](../data-model.md) §3.2）

輸出：blocks 已插入 caption 的 `ExtractedDoc`。

### 3.3 分層歸屬（import-linter 會擋的地方）

| 做什麼 | 住在哪 | 為什麼不能在別處 |
|--------|--------|------------------|
| 從 PDF 取出影像 bytes | `etl/extract/loaders/pdf.py` | 純轉換 |
| OCR（bytes → 文字） | `etl/`（新模組），經 `run_isolated` | 純轉換、不可信輸入要進沙箱 |
| 組成 caption block 的文字 | `etl/` | 純轉換 |
| **寫物件儲存** | `services/` | `etl/artifacts.py` 開頭明文：「`etl/` 是純轉換層，讀寫由 service 負責」 |
| **呼叫 Gateway 產生描述** | `services/` | 見下 |
| **記 `UsageLog`** | `services/` | 見下 |

**最後兩列是同一個理由，而且是硬的**：import-linter 的 contract 註解寫著「usage 的落地由
service 負責（鐵則 2），Gateway 直接寫 DB 就繞過了租戶 filter，而它跑在 worker 裡沒有請求
上下文兜底」。`etl` 與 `ai` 都被禁止 import `repositories`／`apps`。

**所以 caption 不能寫在 loader 裡**——它是一次要入帳的 LLM 呼叫，而 `etl/` 沒有入帳的權利。
`etl/` 至今**一次都沒有 import 過 `ai/`**（contract 允許，但實際不曾發生），本 Feature
**不打算成為第一個**。

### 3.4 冪等

冪等鍵 `(doc_id, doc_version, "image")`，由 `EtlJob` 既有的
`UniqueConstraint(document, doc_version, stage)` 保證。同一版本重跑時該 stage 已
`succeeded` 就整段跳過——**FR-016 的「不重複計費」因此是免費的**，不需要另建計費去重機制。

`reingest` 會把 `doc_version + 1`，所以**刻意的重跑**照樣會重新產生描述。這也是對的。

---

## 4. 物件儲存的新方法

```
def presigned_get_url(key: str, *, expires_seconds: int) -> str: ...
```

**位置**：`backend/core/object_storage.py`（既有唯一入口）。

**必須沿用 `_require_own_key`**：既有的 `put_object`／`get_object`／`delete_object` 每一次
都比對 `tenant-{tenant_id}/` 前綴、不符即 `CrossTenantObjectKeyError`。短效連結是第四個
出口，漏掉這一道等於開一個繞過租戶隔離的後門。

**有效期預設 300 秒**——10 § 的既有設計值（「presigned URL 短時效（上傳 15min / 下載
5min）」），不是本 Feature 發明的數字。

**已知限制（要寫進 plan，不在本 Feature 解決）**：短效連結要能被瀏覽器直接打到，物件儲存
就必須對外可達。開發機是 `127.0.0.1` 沒問題；**正式環境需要反向代理，而那屬 Phase 4**
（13 §4.1 的 F-01 餘項）。

---

## 5. 守門：不得被自動化流程碰到的東西

| 東西 | 規則 |
|------|------|
| 真的 OCR 模型權重 | 測試不得下載。unit 層以固定的小圖與假的 OCR 實作驗流程 |
| 真的 VLM | **一律 `MockVisionProvider`**（憲章原則 IV） |
| `make verify-provider CAPABILITY=vision` | 手動執行、會打真服務，**不得**進 `make test`／`lint`／`smoke`／CI。比照既有三種 |

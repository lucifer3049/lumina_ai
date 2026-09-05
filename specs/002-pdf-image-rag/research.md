# Phase 0 Research: PDF 掃描頁與內嵌圖的檢索（W2 圖片 RAG）

**Feature**: [spec.md](./spec.md) ｜ **Date**: 2026-09-05

本檔記錄 Plan 層的技術裁決。每一條的格式是**決定／理由／被否決的替代方案**；凡是尚未
實測的，另立「**待驗**」一行——依 13 §1.2，文件值是起始點，實測推翻時回寫這裡。

> **本檔不改變任何需求語意**（憲章原則 VI）。spec 的 FR 若在此處看起來不可行，處置是
> 停下回報，不是在這裡重新定義它。

---

## R-01：OCR 引擎

**決定**：**RapidOCR**（ONNX Runtime 後端、Apache-2.0），中英雙語、離線、CPU 可跑。

**理由**：

- **它的執行形狀符合既有的沙箱**。抽取跑在 `forkserver` 子行程裡、位址空間上限 1 GiB
  （`etl/extract/sandbox.py` 的 `RLIMIT_AS`）。RapidOCR 是純 ONNX Runtime，安裝體積約
  80 MB、無 PaddlePaddle 相依，塞得進那個上限；PaddleOCR 的完整相依鏈塞不進去。
- **它用的是 PaddleOCR 的模型權重**，中日韓的辨識品質屬同一水準，差的是週邊（版面分析、
  表格還原）——而那些正是本 Feature 的 Out of Scope。
- **授權相容**：Apache-2.0，多租戶 SaaS 無虞。

**被否決的替代方案**：

| 方案 | 否決理由 |
|------|----------|
| PaddleOCR | 準確度與版面能力更好，但相依鏈（PaddlePaddle）重、實務上要 GPU 才划算——而這台機器的 GPU 已經是三方搶（見 R-04）。把 OCR 也放上 GPU 會讓 caption 更擠 |
| Tesseract | 最輕、無 GPU，但用的是傳統 LSTM 架構，中文直排與低品質掃描的表現明顯落後。掃描件正是它最弱的場景 |
| PaddleOCR-VL（2026-01） | 把 OCR 與描述併成同一個視覺模型，看似省一步。否決理由是**它把兩件事綁死**：OCR 失敗與描述失敗從此無法分別處置，而 FR-006（單張失敗不拖垮整份）需要它們可分。且它仍要 GPU，R-04 的排擠問題一點沒解決 |
| 雲端 OCR（各家 Document AI） | 與本 Feature 的動機相反（W2 之所以現在做，前提就是成本壓得住），且掃描件常含個資，送出去是另一個層級的決定 |

**待驗**：① 中文掃描頁的實際辨識率與單頁耗時（GPU 不參與，純 CPU）；② RapidOCR + ONNX
Runtime 在 `RLIMIT_AS = 1 GiB` 下是否跑得動——模型與 runtime 合計預估 300–500 MB，但
ONNX Runtime 的 arena 配置可能把位址空間吃得比常駐記憶體多得多。**②不過就要改的是沙箱的
記憶體上限，不是換引擎**。

---

## R-02：OCR 的執行單位——每張圖一次沙箱呼叫，不是每份文件一次

**決定**：圖片處理**自成一個 ETL stage**（`image`），在該 stage 內**逐張**呼叫
`run_isolated`，每張各自帶逾時；**不**把 OCR 放進既有的 `extract_isolated` 那一次呼叫裡。

**理由**：既有的抽取是**一份文件一次呼叫**（`services/knowledge/ingestion.py::_extract_stage`
→ `extract_isolated(content, media_type=...)`），預算 `EXTRACT_TIMEOUT_SECONDS = 120.0`。
一份 300 頁的掃描件，即使每頁 OCR 只要 0.3 秒也是 90 秒，加上原本的解析必然逼近或超過
120 秒——而**逾時的症狀是「文件永遠處理不完」**：`acks_late` 把訊息還回佇列，下一次重跑
再逾時一次，退避三輪之後才失敗。這正是 W1 的 TEI 批次上限那個坑的同一種形狀（每次重試
都得到同樣的失敗）。

把預算調大不是解法：120 秒擋的是畸形檔的無限迴圈，為了掃描件把它調到十分鐘，等於把那道
防線一起拆了。

**被否決的替代方案**：

| 方案 | 否決理由 |
|------|----------|
| 把 `EXTRACT_TIMEOUT_SECONDS` 調大 | 見上——那個逾時擋的是畸形檔，不是慢檔。兩者需要不同的預算 |
| OCR 完全不進沙箱（在 worker 行程內直接跑） | 影像也是不可信輸入；解碼器的 OOM 與無限迴圈同樣不是 `try/except` 接得住的，而 worker 掛掉會停掉那台機器上**所有租戶**的 ETL（08 §6 的原話） |
| 一次沙箱呼叫處理整批圖 | 單張失敗會拖垮整批，與 FR-006 直接衝突 |

---

## R-03：圖片如何離開沙箱

**決定**：`ExtractedDoc` 新增 `images: tuple[ExtractedImage, ...]` 欄位，由 loader 在既有
那一次抽取中一併回傳**已編碼的影像 bytes 與它的位置**；service 收到後寫進物件儲存。
單張與單份都設上限（R-12），超過上限的只回傳位置與「因上限略過」的標記，不回傳 bytes。

**理由**：

- **不能讓 service 在沙箱外重新開一次 PDF**。那等於讓不可信的 PDF 在 worker 主行程裡被
  解析第二次，沙箱的意義當場歸零。
- 既有的跨行程傳輸就是 pickle 走 `Pipe`（`sandbox.py`），dataclass 本來就傳得過去；
  `ExtractedDoc` 是 `frozen=True, slots=True`，加一個欄位不破壞既有形狀。
- **`etl/` 不碰物件儲存**是明文規則（`etl/artifacts.py` 開頭：「這裡不碰物件儲存：`etl/`
  是純轉換層，讀寫由 service 負責」）。所以 loader 只能把 bytes 交出來，寫入由 service 做。

**風險與緩解**：影像 bytes 走 pickle 會同時吃掉子行程與父行程的記憶體，而子行程有 1 GiB
的 `RLIMIT_AS`。緩解是 R-12 的兩道上限（單張大小、單份張數），且**上限在 loader 內生效**
——不是先全部抽出來再丟掉。

**待驗**：pdfplumber 是否能直接取得內嵌影像的原始 bytes，以及能否把整頁點陣化（掃描頁需要
後者）。若整頁點陣化需要額外的相依（pypdfium2 之類），那是一個**新的外部相依**，要回報後
才加——它不在 spec 的 Dependencies 裡。

---

## R-04：描述生成（caption）的模型與硬體配置

**決定**（2026-09-05 第二次修訂，人類裁決改用 vLLM）：走**地端 GPU**，以 **vLLM** 服務，
在 `VENDORS` 新增 `vllm` 一列（`http://127.0.0.1:${VLLM_PORT}/v1`、免金鑰），模型取
**2B 等級的視覺模型、AWQ 或 GPTQ 權重**；容器放 `gpu` profile，**要用才起**
（`make vllm-up`／`-down`／`-logs`，形狀比照既有兩個 TEI 容器）。

### 硬體實測（2026-09-05，這一節的所有結論都建立在這三個數字上）

| 量到的 | 值 |
|--------|-----|
| GPU | RTX 5060，**8151 MiB 總量、5057 MiB 已用**（兩個 TEI 容器 up 且 healthy）→ **剩 3.0 GB** |
| CPU | i5-9400F，6 核 6 執行緒 @ 2.9 GHz，**指令集只到 AVX2，沒有 AVX512** |
| 宿主記憶體 | 31.92 GB；**WSL 原本只吃得到 10 GB**（`.wslconfig` 的 `memory=10GB`，2026-08-24 為修 WSL 崩潰而設，當時宿主 16 GB），2026-09-05 改為 24 GB |

**理由**：

- **剩 3.0 GB 這個數字推翻了本節的第一版**。第一版寫「4B 的 Q4 約 3 GiB，是唯一能共存的
  量級」——實測剩 3.0 GB，而 4B 的權重加上 KV cache、再加上視覺編碼器處理高解析頁面時的
  活化記憶體會要到 4 GB 上下。**放不下。** 能穩妥共存的是 2B 等級（量化後約 1.5–2 GB）。
- **vLLM 的連續批次正對這個工作負載**：caption 是離線批次，一份文件幾十張圖一次送進去，
  批次吞吐遠優於逐張處理。
- **它必須是 profile 容器，因為 vLLM 不會用完卸載**：它按 `gpu_memory_utilization` 預先開
  一塊 VRAM 池並持有到行程結束（沒有 Ollama `keep_alive` 的等價物）。在只剩 3.0 GB 的卡上
  常駐第三個服務不可行，所以要用才起——**而這反而讓形狀更一致**，那正是兩個 TEI 容器已經
  在用的模式。
- **Gateway 側是加一列，不是加一個實作**：vLLM 是 OpenAI 相容，`VENDORS` 加 `vllm` 一列即可
  （與 `tei` 那列同形）。

**被否決的替代方案**：

| 方案 | 否決理由 |
|------|----------|
| **CPU 推論（用那 32 GB 記憶體）** | **這顆 CPU 只有 AVX2、沒有 AVX512**（實測），而 vLLM 的 CPU 後端是針對 AVX512 做的。即使改用 llama.cpp 這類 AVX2 可跑的執行器，估算也是**每張圖 1–2 分鐘**——VLM 讀一張掃描頁會產生上千個視覺 token 全部進 prefill，那是純算力密集，6 核 2.9 GHz 沒有取巧空間。一份 100 張圖的文件要 2–3 小時 |
| Ollama（第一版的決定） | 人類 2026-09-05 裁決改 vLLM。客觀差異：Ollama 有 `keep_alive` 可閒置卸載、吃 GGUF、AVX2 可跑 CPU；vLLM 吃 AWQ/GPTQ、批次吞吐好得多、但持有 VRAM 且 CPU 路實質不可用。**批次吞吐對離線 caption 是主要指標**，這個交換成立 |
| 雲端 VLM | 每張圖一次雲端呼叫，且掃描件常含個資。與 W2 現在才排上來的理由（成本壓得住）相反 |
| 4B 或 8B 的量化版 | 8B-Q4 約 6 GB、4B 約 4 GB，**都超過實測的 3.0 GB 餘裕** |
| 讓 TEI 容器在 ETL 期間停掉，跑完再起 | 停掉 `tei-embed` 等於停掉 embedding，而 ETL 的下一步就是 embedding；停掉 `tei` 等於同一段時間所有租戶的檢索品質掉一級。省 VRAM 的代價是讓兩件事互相踩 |

**待驗**：

① **2B 等級模型描述中文圖表的品質是否堪用**——這是本 Feature 最大的未知數。它很可能產出
「一張包含多個方框與箭頭的示意圖」這種**正確但無用**的句子，而那種描述進了索引不只沒幫助，
還會稀釋檢索結果。**這一項不過的話，處置是回報**（更大的模型放不下，見上表）。
② vLLM 在 **Blackwell（sm_120）+ WSL2** 上跑不跑得起來。這個 repo 已為 TEI 打過同一場仗
（`120` 標籤 ＋ tmpfs 蓋 `/usr/local/cuda/compat`）；vLLM 需要 CUDA 12.8 以上的建置，**不
假設它會順**。
③ 實際 VRAM 佔用與單張耗時（靠 R-05 的新 capability 量）。

> **①可以在寫任何程式之前就測掉**：起一個 vLLM 容器、丟幾張真的架構圖進去，看它寫什麼。
> 那半小時決定的是後面幾天的方向。

---

## R-05：Gateway 的第四種能力怎麼接——分兩段，不要一次動到 chat

**決定**：

1. **caption 生成（US2）走一個新的 `VisionProvider` Protocol**，方法是
   `describe_image(image: bytes, *, media_type: str, prompt: str, model: str, timeout_seconds: float)`，
   配一組**獨立的設定**（`ai_vision_provider` / `ai_vision_model` / `ai_vision_base_url` /
   `ai_vision_timeout_seconds`）。**完全不動 `ChatMessage`。**
2. **US6（追問時把原圖交給模型）才擴充 `ChatMessage.content`**，由 `str` 改為
   `str | Sequence[ContentPart]`。

**理由**：

- 既有的三種能力（embedding／chat／rerank）就是**三組獨立的 Protocol + 三條獨立的工廠 +
  三組獨立設定**，`app_settings.py` 的註解明講理由是「選型依據不同」。caption 的選型依據
  （能看圖、跑得動、便宜、慢一點沒關係）與對話（TTFT、串流、工具呼叫）**完全不同**，
  第四組獨立設定是照著既有理由走，不是新發明。
- **`ChatMessage.content: str` 是整條問答路徑的地基**：SSE 的七種事件、usage 的結算、
  prompt 的版本化快照全部長在它上面。為了一個 P3 的故事去改它，等於讓 US2 的交付綁上
  chat 路徑的迴歸風險。分開之後，US6 可以整段延後而 US1–US5 不受影響——這與 spec 把 US6
  排在 P3、並在 Assumptions 標明「唯一需要新增看圖能力的部分」是同一個判斷。
- 鐵則 5 不受影響：兩段都在 `ai/gateway/` 內，呼叫端一律經 `AIGateway`。

**被否決的替代方案**：

| 方案 | 否決理由 |
|------|----------|
| 一次就把 `ChatMessage.content` 改成多模態，caption 也走 `stream_chat` | 少一個 Protocol，但 caption 不需要串流、不需要工具呼叫、不需要 fallback 鏈，卻要承擔那條路徑的全部複雜度；且把 US2 的交付綁死在 chat 型別變更的迴歸風險上 |
| caption 直接打 Ollama，不經 Gateway | 違反鐵則 5（LLM 呼叫只准經 AI Gateway）。無討論空間 |
| 復用 `rerank` 那種「自成一格的 adapter」形狀 | 正是這麼做——`VisionProvider` 與 `RerankProvider` 同構（獨立 Protocol、獨立設定、不共用 `VENDORS`）。此列保留是為了標明：這不是新形狀 |

**同時要補**：`scripts/verify_provider.py` 的 `--capability` 加 `vision`（既有三種各有一套
驗法）。R-04 的兩個待驗項要靠它才量得到。

---

## R-06：caption 的 prompt

**決定**：走既有的 DB 版本化模板，新增系統模板 `key = "image_caption"`，以 seed migration
落地（形狀比照 `apps/ai/migrations/0004_seed_rag_prompt.py` 的 `rag_answer`）。

**理由**：鐵則 5 的後半——「Prompt 一律經 PromptBuilder 使用版本化模板，禁止散落 Python
string」。caption 的提示詞會反覆調整（描述多長、要不要照抄圖上文字、遇到純裝飾圖要說
什麼），而**調整的效果只能靠比較兩次產出來判斷**——沒有版本號就沒得比。

**附帶**：`PromptVersion` 已有 `model_hint` 欄位，caption 模板可用它記下當初是為哪個模型
寫的——4B 與 8B 對同一段提示詞的服從度不同，換模型時這個欄位是唯一線索。

---

## R-07：用量與成本怎麼記

**決定**：

- `UsageEvent.category` 新增 `"vision"`（既有值：`"llm"`、`"embedding"`）。
- `request_id` 用 `f"caption:{document_id}:v{doc_version}:{image_seq}"`，沿用
  `services/knowledge/embedding.py` 的 `f"embed:{document_id}:v{doc_version}"` 形狀。
- 呼叫端是 **service**（`services/knowledge/` 底下），不是 `etl/`、也不是 Gateway。

**理由**：`ai` 與 `etl` 都被 import-linter 禁止 import `repositories`／`apps`，而理由寫在
contract 的註解裡：「usage 的落地由 service 負責（鐵則 2），Gateway 直接寫 DB 就繞過了租戶
filter，而它跑在 worker 裡沒有請求上下文兜底」。**這條規則直接決定了 caption 不能寫在
loader 裡**——它是一次要入帳的 LLM 呼叫，而 `etl/` 沒有入帳的權利。

**成本值得先講清楚**：`compute_cost` 查不到價目時回 `None`（不是 0），而地端模型本來就不在
`ai_model_prices` 裡。所以 caption 的 `cost` 會是 `None`——**這是誠實的**（那一格的意思是
「沒有價目」而不是「免費」；GPU 電費與排擠成本是真的）。同時，`tokens_month` 這個配額只
認 `category="llm"`，所以 caption 不吃它——與 embedding 的處置一致。

---

## R-08：短效授權連結

**決定**：在 `core/object_storage.py` 新增 `presigned_get_url(key: str, *, expires_seconds: int) -> str`，
**沿用既有的 `_require_own_key` 前綴檢查**；有效期預設 **300 秒**；「這個使用者能不能看這份
文件」的授權判斷留在 service，core 只負責「這個 key 屬不屬於當前租戶」。

**理由**：

- **有效期不是新發明的值**：10 §「物件儲存 | MinIO per-tenant prefix + presigned URL
  短時效（上傳 15min / 下載 5min）」——下載 5 分鐘是既有設計值，照抄。
- **兩層檢查各在各的位置**：`core` 已經對每一次 `put/get/delete` 強制比對
  `tenant-{tenant_id}/` 前綴並 raise `CrossTenantObjectKeyError`，短效連結沿用同一道；而
  「文件讀取權」是業務規則，屬 service。把後者塞進 core 會讓 core 認識權限模型，違反
  「core 是最內圈」。

**被否決的替代方案**：

| 方案 | 否決理由 |
|------|----------|
| 由 API 代理圖片位元組（`GET /documents/{id}/images/{n}`） | 權限檢查確實更集中，但圖片流量會全部穿過 FastAPI；且 10 § 已經定案走 presigned。要推翻它需要的是修改 10，不是在 plan 裡繞過 |
| 長效或不過期的連結 | 圖片內容可能含個資（spec 的 Edge Cases 已記）。一條貼出去就永久有效的連結，等於把租戶隔離降級成「別把網址給別人」 |

**前提（需回報而非默默假設）**：presigned URL 要能被瀏覽器直接打到，物件儲存就必須對外
可達。開發機是 `127.0.0.1` 沒問題；**正式環境需要反向代理**，而反向代理屬 Phase 4
（13 §4.1 的 F-01 餘項）。本 Feature **不建那個代理**——這一條要寫進 plan 的已知限制。

---

## R-09：圖片段落如何標示，以及標示怎麼一路帶到 citation

**決定**：

- `Chunk.meta` 增兩個鍵：`generated: true`（這段是機器產生的描述）與 `image_key`（原圖在
  物件儲存的 key）。既有的 `meta` 只有 `page` 與 `heading_path` 兩鍵，加鍵不需要 migration
  （`JSONField`）。
- **`rag.retrievers.vector.RetrievedChunk` 也要增兩個欄位**：`generated: bool = False` 與
  `image_key: str | None = None`。這一步不能省——`RetrievedChunk` **不帶** chunk 的 `meta`
  dict，`page` 與 `heading_path` 是在 `to_retrieved()` 就攤平出來的，之後整條鏈（citation、
  context、SSE、前端）**沒有任何通用的 `meta`／`extra` 欄位可以搭便車**。要多帶什麼，就得
  在那裡多攤平一個欄位。
- `rag.citation.Citation` 增兩個欄位並進 `as_dict()`：`generated: bool` 與
  `image: {...} | None`。
- 09 §3.2 的 `citations` 事件因此擴充兩個欄位——**這是相容擴充**（既有九個欄位不動），
  且該事件早在 1D-5 就已經是物件 `{"items":[...]}` 而不是裸陣列，新增欄位不需要改形狀。

**理由**：FR-008 要求生成標示「從產生的那一刻起一路保留到使用者看到的引用上，中途不得
遺失」。這條鏈上唯一能承載它的載體是 `Chunk.meta` → `RetrievedChunk` → `Citation`，而
`page`／`heading_path` 已經逐字走過同一條路——照抄它們的做法即可。

**契約後果（比預期小，但有一處是真的）**：

- **`Citation` 加欄位不會造成 OpenAPI 漂移**。`MessageOut.citations` 的型別是
  `list[dict[str, Any]]`（jsonb 原樣透傳，沒有逐鍵的 pydantic 模型），codegen 產出的 TS 是
  `{[key: string]: unknown}[]`。前端那份 `CitationItem`（`frontend/src/utils/citations.ts`）
  是**手寫**的 interface，本來就帶 `[key: string]: unknown`。所以這一段是加欄位、手改前端
  型別與 `CitationPanel.vue`，**不是契約變更**。
- **真正會改契約的是換圖片連結的那支新端點**——它會新增 `operation_id`、權限 code 與
  OpenAPI schema，因此 `make openapi && make gen-api` **兩段都要跑**（憲章原則 V：單跑
  `gen:api` 會用舊契約重產一次而看到假的 no diff）。

**待驗 → 已定案**：`image` 欄位**不放短效連結，只放識別資訊**（`image_key` 與尺寸之類），
由前端在使用者真的展開引用時另打一支端點換連結。理由有二：① 多數引用不會被點開，檢索當下
就燒連結等於每次問答產生一批 5 分鐘後就作廢的授權；② 連結一旦進了 `messages.citations`
這個 jsonb 欄位就**被永久存進資料庫**——那是一則歷史訊息，而它會帶著一個當時有效的授權
字串躺在那裡。第二點才是決定性的。

---

## R-10：「重新抽取」不是新機制——它已經存在

**決定**：FR-018 的逐 KB 重新抽取，**沿用既有的 `DocumentService.reingest`**，本 Feature
只加一層 KB 範圍的批次觸發與進度查詢（新表 `kb_reextract_jobs` + 新端點）。

**理由**：`POST /documents/{id}/reingest` 已經存在，且它的三件事正好就是 FR-018 要的：
`ready → parsing`、`doc_version + 1`（冪等鍵的一部分）、**舊 chunk 標 `superseded` 而不是
刪除**。最後那一項直接滿足 FR-019 的「重跑期間檢索不空窗」——不是本 Feature 要新做的保證，
是既有語意本來就給的。

**為什麼不共用 `kb_reindex_jobs`**：2B-6 已經裁決過同一類問題——「重切與換模型不得是同一個
job」（回 422，請分兩次跑）。`kb_reindex_jobs` 的欄位是為 embedding 版本切換設計的
（`target_model`、`target_version`、`switched_at`），重新抽取一個都用不到；硬塞進去會讓
一半的欄位對一半的 job 沒有意義，而那種表最後一定會有人讀錯。**形狀對齊、資料分開。**

**被否決的替代方案**：讓維護者自己逐份打 `POST /documents/{id}/reingest`。否決理由是
FR-019 要求可查詢進度——幾百份文件的進度不能靠人自己數。

---

## R-11：冪等鍵

**決定**：新增 ETL stage `image`，冪等鍵沿用既有的 `(doc_id, doc_version, stage)`，由
`EtlJob` 的 DB 唯一約束保證（`repositories/knowledge.py::EtlJobRepository.start` 走
`get_or_create` + 唯一約束，並發安全）。

**理由**：FR-016 要的「重複處理不重複計費」，在既有機制下是免費的——同一個
`(doc_id, doc_version, "image")` 若已 `succeeded`，這一輪整段跳過，caption 一次都不會發。
`reingest` 會把 `doc_version + 1`，於是**刻意的重跑**照樣會重新產生描述，這也是對的。

**stage 的位置**：排在 `extract` 之後、`clean` 之前。理由是 clean 會落中間產物
（`{storage_key}/v{doc_version}/cleaned.json`），而斷點續跑正是從那份產物讀回來的——圖片
產生的文字必須在它落地**之前**併進 blocks，否則續跑會拿到一份沒有圖片內容的產物，而且
看起來完全正常。

---

## R-12：成本與資源護欄的起始值

**決定**（全部可設定，以下是起始點不是定論）：

| 參數 | 起始值 | 依據 |
|------|--------|------|
| 單份文件最多處理幾張圖 | 100 | 一份 100 張圖的文件已經是長尾；配上 4B 模型單張數秒，整份落在分鐘級 SLO 內 |
| 單張影像大小上限 | 8 MiB | 沙箱 `RLIMIT_AS` 是 1 GiB，而 pickle 會讓 bytes 在父子行程各存一份 |
| 單張 OCR 逾時 | 30 秒 | 遠大於 CPU OCR 單頁的預期（次秒級），但擋得住解碼器的病態輸入 |
| 單張 caption 逾時 | 60 秒 | 地端 4B 模型冷啟動（模型載入）要算在內 |
| 短效連結有效期 | 300 秒 | 10 § 的既有設計值（下載 5min） |

**這一整張表都需要實測校正**，依 13 §1.2「文件值是起始點，調整需在 PR 說明中標注並引用
依據」。**唯一有既有依據的是最後一列。**

---

## 待驗項彙總（開工前或開工中必須關掉的）

| # | 待驗 | 來源 | 不過的處置 |
|---|------|------|------------|
| 1 | RapidOCR 在 `RLIMIT_AS = 1 GiB` 下跑得動 | R-01 | 調沙箱記憶體上限，**不換引擎** |
| 2 | 中文掃描頁的辨識率與單頁耗時 | R-01 | 回報，由人決定是否改走 GPU OCR |
| 3 | pdfplumber 能否取內嵌影像 bytes、能否整頁點陣化 | R-03 | 需要新相依就**停下回報**（不在 spec 的 Dependencies 內） |
| 4 | Qwen3-VL-4B-Q4 的實際 VRAM 與單張耗時 | R-04 | 回報，**不自行換 8B**（會推翻 VRAM 結論） |
| 5 | 中文圖表的描述品質是否堪用 | R-04 | 同上 |

> **R-09 原本列為待驗的第 6 項（連結即時產生 vs 另打端點）已於本檔內定案**，理由是
> `messages.citations` 是會被永久保存的 jsonb 欄位——把授權字串寫進去是把短效連結變成
> 歷史紀錄的一部分。

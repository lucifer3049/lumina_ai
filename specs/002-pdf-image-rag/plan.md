# Implementation Plan: PDF 掃描頁與內嵌圖的檢索（W2 圖片 RAG）

**Branch**: `002-pdf-image-rag` | **Date**: 2026-09-05 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/002-pdf-image-rag/spec.md`

---

## Summary

讓 PDF 裡的掃描頁與內嵌圖變得檢索得到。做法是**把圖片的內容變成文字**（OCR 取字 ＋ VLM
產生描述 ＋ 前後文），以既有的 `BlockType.CAPTION` 併進 blocks，走完既有的 clean → chunk →
embed；原圖留在物件儲存，由 `Chunk.meta.image_key` 指回去。**圖片本身不進向量空間**，
檢索路徑數量不變。

技術路線的四個支點（依據見 [research.md](./research.md)）：

1. **OCR 用 RapidOCR**（ONNX、Apache-2.0、CPU 可跑），**每張圖各自一次沙箱呼叫**——既有的
   抽取是「一份文件一次、預算 120 秒」，掃描件放進去必然逾時，而逾時的症狀是文件永遠處理
   不完。
2. **描述生成走地端 GPU、以 vLLM 服務、模型取 2B 等級（AWQ/GPTQ）**——實測卡上只剩
   **3.0 GB**（兩個 TEI 容器佔了 5057 MiB／8151 MiB），4B 以上放不下；而 CPU 只有 AVX2
   沒有 AVX512，跑 VLM 每張圖要 1–2 分鐘，那條路不可用。vLLM 不會閒置卸載，因此比照兩個
   TEI 容器放 `gpu` profile、要用才起。
3. **Gateway 新增第四種能力 `VisionProvider`，完全不動 `ChatMessage`**；US6（追問時把原圖
   交給模型）才動它，因此可整段延後而不影響 US1–US5。
4. **「重新抽取」不是新機制**——`POST /documents/{id}/reingest` 已經存在，本 Feature 只加
   一層逐 KB 的批次觸發與進度查詢。

---

## Technical Context

**Language/Version**: Python 3.12（uv）／TypeScript strict（Vue 3 + Pinia + pnpm）

**Primary Dependencies**: FastAPI（唯一 HTTP 入口）、Django 5 ORM＋Migration、Celery + Redis、
PostgreSQL 16 + pgvector(halfvec 1024) + pgroonga、MinIO（boto3 S3 API）、pdfplumber。
**本 Feature 新增**：RapidOCR（ONNX Runtime）；**可能新增**：整頁點陣化所需的相依
（research 待驗項 ③ — **需要就停下回報**，它不在 spec 的 Dependencies 裡）。

**Storage**: PostgreSQL（一張新表 `kb_reextract_jobs`；其餘全部沿用既有欄位）＋ MinIO
（原圖，key 形狀沿用既有中間產物）

**Testing**: pytest 四層（unit／integration／api／e2e）＋ `make smoke`。
**LLM 一律 `MockVisionProvider`**（憲章原則 IV）。

**Target Platform**: Linux server（開發機為 WSL2 + RTX 5060 8 GiB）

**Project Type**: Multi-tenant SaaS web service（Modular Monolith）

**Performance Goals**: ETL 是分鐘級 SLO（非互動路徑）。單張 OCR < 30 秒、單張描述 < 60 秒
（皆為逾時上限，非目標值）。**檢索與問答的延遲預算完全不受本 Feature 影響**——一般提問
送給模型的內容中圖片數為 0（FR-012 / SC-011）。

**Constraints**（2026-09-05 實測，非估計）：抽取子行程 `RLIMIT_AS = 1 GiB`、單次沙箱呼叫
120 秒；**GPU 8151 MiB 中已用 5057 MiB（兩個 TEI 容器），只剩 3.0 GB**；**CPU 只有 AVX2、
無 AVX512**，因此 CPU 推論不是可用選項；WSL 記憶體上限 10 GB → 24 GB（宿主 31.92 GB）；
短效連結 300 秒（10 § 既有設計值）。

**Scale/Scope**: 單份文件圖片上限 100 張、單張影像 8 MiB（皆為 research R-12 的起始值，
需實測校正）。

---

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Verdict | Notes |
|-----------|---------|-------|
| I. 單一入口與單向分層 | **PASS** | 三支新端點皆為 FastAPI 三行 controller；新 Celery task 為薄包裝。**設計刻意把「寫物件儲存／呼叫 Gateway／記 UsageLog」放在 `services/`，把 OCR 與文字組裝放在 `etl/`**——`etl` 與 `ai` 都被 import-linter 禁止 import `repositories`／`apps`（見 contracts/internal.md §3.3）。`etl/` 至今未曾 import `ai/`，本 Feature 不成為第一個 |
| II. 租戶隔離 Fail Fast | **PASS** | 新 repository 繼承 `TenantScopedRepository`；`presigned_get_url` 沿用 `core/object_storage.py` 既有的 `_require_own_key` 前綴檢查；圖片 key 帶 `tenant-{tenant_id}/` 前綴（既有 `build_document_key` 的形狀）；換連結的端點以 404 而非 403 回應跨租戶請求 |
| III. AI 呼叫收斂於 Gateway | **PASS** | caption 經 `AIGateway.describe_image()`；provider adapter 住在 `ai/gateway/providers/vision.py`；prompt 走 DB 版本化模板 `image_caption`（seed migration），不散落 Python string |
| IV. 驗收測試先行與四層測試 | **PASS** | DoD 逐條回溯 spec 的 Acceptance Criteria（見 quickstart.md 的對照欄）。四層都有：unit（OCR 組裝、caption block、citation 欄位）、integration（新表 + RLS、物件 key 的租戶檢查）、api（三支端點的權限矩陣與 409/404）、e2e／smoke（既有路徑不得迴歸） |
| V. 契約與結構變更受控 | **PASS** | 唯一的 schema 變更是**新建表**（`CreateModel`，不涉三步走、不需 `AddIndexConcurrently`）。三支新端點同步權限 code（`knowledge:read`／`knowledge:admin`）、`operation_id`、審計事件（`knowledge_base.reextract`），並跑 `make openapi && make gen-api` 兩段 |
| VI. 規格先行與分層授權 | **PASS** | 本 plan 不改變 spec 的任何需求語意。research 的每一條裁決都對應得到 FR |

**Does this plan restate or alter any requirement in `spec.md`?** No.

**兩件在設計中浮現、屬於「回報而非自行決定」的事**（已寫進對應產物，不在此解決）：

1. **整頁點陣化可能需要一個新的外部相依**（research 待驗項 ③）。spec 的 Dependencies 只
   列了「影像文字辨識」與「具備看圖能力的模型」兩項；若 pdfplumber 取不到點陣圖而需要
   第三個套件，那是 Dependencies 的增補，**停下回報**。
2. **正式環境的短效連結需要反向代理**（contracts/internal.md §4），而反向代理屬 Phase 4
   （13 §4.1 的 F-01 餘項）。**本 Feature 不建它**——這是已知限制，不是缺陷。

---

## Project Structure

### Documentation (this feature)

```text
specs/002-pdf-image-rag/
├── plan.md              # 本檔
├── spec.md              # 已 review
├── research.md          # Phase 0（12 條裁決 + 6 項待驗）
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   ├── api.md           # HTTP 端點與 SSE 事件
│   └── internal.md      # Gateway 能力、ETL stage、物件儲存
└── tasks.md             # Phase 2（/speckit-tasks 產出，本指令不建）
```

### Source Code (repository root)

**Structure Decision**——本 Feature 動到的檔案，依既有目錄分組：

**`backend/etl/`（純轉換，不碰儲存、不碰 ORM、不碰 Gateway）**

| 檔案 | 變動 |
|------|------|
| `extract/model.py` | 新增 `ExtractedImage`；`ExtractedDoc` 加 `images` 欄位（帶預設，既有四個 loader 不動） |
| `extract/loaders/pdf.py` | 取出內嵌圖與掃描頁的影像；「無文字層即失敗」改為「無文字層**且**無影像可處理才失敗」 |
| `extract/isolated.py` | 新增逐張影像的沙箱入口與它自己的逾時常數（**不動** `EXTRACT_TIMEOUT_SECONDS`） |
| `ocr.py`（新） | RapidOCR 封裝，模組層級函式（forkserver 要求可 pickle） |
| `imaging.py`（新） | OCR 文字 ＋ 描述 ＋ 前後文 → `Block(type=CAPTION)` |
| `artifacts.py` | `FORMAT_VERSION` 1 → 2（影像 bytes **不**進中間產物） |

**`backend/ai/`**

| 檔案 | 變動 |
|------|------|
| `gateway/providers/__init__.py` | 新增 `VisionProvider` Protocol 與 `ProviderVision` |
| `gateway/providers/vision.py`（新） | OpenAI 相容的多模態 adapter |
| `gateway/providers/openai_compatible.py` | `VENDORS` 加 `vllm` 一列（形狀同 `tei`：免金鑰、`supports_dimensions=False`） |
| `gateway/providers/mock.py` | 新增 `MockVisionProvider`（決定性輸出） |
| `gateway/__init__.py` | `AIGateway.describe_image()` ＋ `_vision_provider()` 工廠 |

**`backend/services/`（唯一能寫儲存、呼叫 Gateway、記 UsageLog 的層）**

| 檔案 | 變動 |
|------|------|
| `knowledge/imaging.py`（新） | image stage 的編排：寫物件儲存 → 逐張 OCR → 逐張 caption → 記 `UsageLog(category="vision")` → 組回 blocks |
| `knowledge/ingestion.py` | 插入 `image` stage（**排在 `clean` 之前**，理由見 contracts/internal.md §3.1） |
| `knowledge/reextract.py`（新） | 逐 KB 的批次重跑，內部逐份呼叫既有的 `DocumentService.reingest` |
| `knowledge/documents.py` | 換圖片連結時的文件讀取權判斷 |
| `platform/usage.py` | 無程式變動；`category="vision"` 是新值不是新欄位 |

**`backend/repositories/`**

| 檔案 | 變動 |
|------|------|
| `knowledge.py` | 新增 `KbReextractJobRepository`（繼承 `TenantScopedRepository`） |

**`backend/apps/`**

| 檔案 | 變動 |
|------|------|
| `knowledge/models.py` | 新增 `KbReextractJob`（薄：欄位、Meta、`__str__`） |
| `knowledge/migrations/00XX_kb_reextract_job.py`（新） | 只有 `CreateModel` |
| `ai/migrations/00XX_seed_image_caption_prompt.py`（新） | seed `image_caption` 系統模板 |

**`backend/api/`**

| 檔案 | 變動 |
|------|------|
| `v1/knowledge.py` | 三支端點（`documents_image_url`、`knowledge_bases_reextract`、`knowledge_bases_reextract_status`）——`/documents` 與 `/knowledge-bases` 同檔是既有安排 |
| `schemas/knowledge.py` | `ImageUrlOut`、`KbReextractJobOut` |
| `middleware/audit.py` | 一列：`knowledge_base.reextract` |

**`backend/core/`／`backend/worker/`**

| 檔案 | 變動 |
|------|------|
| `core/object_storage.py` | `presigned_get_url()`，沿用 `_require_own_key` |
| `core/tasks.py` | `enqueue_reextract()` |
| `worker/reextract_tasks.py`（新） | 薄包裝，形狀比照 `reindex_tasks.py` |

**`backend/config/`**

| 檔案 | 變動 |
|------|------|
| `settings/app_settings.py` | 第四組 AI 設定 `ai_vision_*`；圖片護欄四個參數（R-12） |

**repo 根與 `docker/`（基礎設施）**

| 檔案 | 變動 |
|------|------|
| `docker/compose.yml` | 第三個 `gpu` profile 容器 `vllm`（`${VLLM_PORT}`、獨立 volume、`gpu_memory_utilization` 需夾在實測的 3.0 GB 餘裕內、healthcheck 的 `start_period` 要涵蓋首次下載——W1 的 36 分鐘教訓） |
| `Makefile` | `vllm-up`／`vllm-down`／`vllm-logs`，形狀比照既有的 `tei-*` 與 `tei-embed-*` |
| `.env.example` | `VLLM_PORT` ＋ 連接埠警告（8070–8169 落在 Hyper-V 保留區間，W1 已踩過兩次） |

**`backend/rag/`**

| 檔案 | 變動 |
|------|------|
| `retrievers/vector.py` | `RetrievedChunk` 加 `generated`／`image_key`（由 meta 攤平） |
| `citation.py` | `Citation` 加 `generated`／`image`，並進 `as_dict()` |

**`frontend/`**

| 檔案 | 變動 |
|------|------|
| `src/utils/citations.ts` | `CitationItem` 加兩個欄位（**手寫檔，不是 codegen 產物**） |
| `src/components/chat/CitationPanel.vue` | 生成標示；展開時換連結並顯示原圖 |
| `src/services/`、`src/stores/` | 換連結的呼叫（views 不直接 fetch） |
| `src/api/generated/` | **由 `make gen-api` 重產，禁止手改** |

**`backend/tests/`**：unit（`test_etl_images.py`、`test_ocr.py`、`test_vision_provider.py`、
`test_citation.py` 擴充）／integration（`test_kb_reextract.py`、`test_object_storage_presigned.py`、
`test_rls_knowledge.py` 擴充）／api（`test_document_image_endpoints.py`、
`test_kb_reextract_endpoints.py`、`test_knowledge_permissions.py` 擴充）。

### 明確不動的目錄

`backend/tool/`、`backend/common/`、`backend/services/conversation/`（US6 之外）、
`backend/services/rag/`、`backend/api/v1/` 的其餘檔案、所有 `/analytics/*`、
`backend/evaluation/`（本 Feature 不跑評測）。

**`backend/services/conversation/` 與 `backend/ai/gateway/chat.py` 只有 US6 才會動到**
——那是 P3，且它是唯一需要把 `ChatMessage.content` 由 `str` 改成多模態的部分。
把它排在最後，`ChatMessage` 的變更風險就不會壓在 US1–US5 的交付上。

---

## Complexity Tracking

> Constitution Check 無 violation，本節不適用。

留兩條說明，避免日後被當成沒有理由的複雜度：

| 看起來多餘的東西 | 為什麼需要 | 更簡單的替代方案為何被否決 |
|------------------|------------|---------------------------|
| Gateway 的第四種能力（`VisionProvider`） | caption 的選型依據與對話完全不同（不需串流／工具呼叫／fallback 鏈），而既有三種能力本來就是三組獨立 Protocol＋設定，理由同樣是「選型依據不同」 | 讓 caption 走 `stream_chat`：要先把 `ChatMessage.content` 改成多模態，等於把 US2 的交付綁上整條問答路徑的迴歸風險 |
| 新表 `kb_reextract_jobs` | FR-019 要求可查詢進度，幾百份文件的進度不能靠人數 | 共用 `kb_reindex_jobs`：它的 `target_model`／`target_embedding_version`／`switched_at` 對重新抽取一個都用不到，一半欄位對一半的 job 沒有意義（2B-6 已經為同一類問題裁決過「重切與換模型不得同一個 job」） |

**反向的一筆——本設計刻意少建的東西**：spec 的 Key Entity「圖片產出物」**不建表**。原圖的
key 是決定性的、統計有 `EtlJob.stats` 這個現成的 `JSONField`，而 spec 沒有任何需求要求逐張
查詢圖片。詳見 [data-model.md](./data-model.md) §1。

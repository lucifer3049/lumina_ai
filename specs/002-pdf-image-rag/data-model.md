# Phase 1 Data Model: PDF 掃描頁與內嵌圖的檢索（W2 圖片 RAG）

**Feature**: [spec.md](./spec.md) ｜ **Research**: [research.md](./research.md) ｜ **Date**: 2026-09-05

---

## 0. 這個 Feature 只需要一張新表

盤點之後，spec 的四個 Key Entity 有三個**不需要新的資料表**：

| spec 的 Key Entity | 落點 | 為什麼不建表 |
|--------------------|------|--------------|
| 圖片產出物（Image Artifact） | 物件儲存的決定性 key ＋ `EtlJob.stats` | 見 §1 |
| 由圖產生的段落 | 既有 `Chunk`，`meta` 加兩個鍵 | `meta` 是 `JSONField`，加鍵不需要 migration |
| 引用（Citation，擴充） | 既有 `rag.citation.Citation`，非持久化型別 | 不是資料表 |
| 重新抽取工作 | **新表 `KbReextractJob`** | 見 §4 |

### §1 為什麼圖片產出物不建表

FR-004（原圖可保存且段落追得回）、FR-015（略過數有記錄）、FR-023（每份文件的圖片統計）
這三條，用既有機制就滿足得了：

- **原圖的位置是算得出來的**，不需要查表：
  `{document.storage_key}/v{doc_version}/images/{seq}.{ext}`。這個形狀逐字沿用既有的中間
  產物 key（`{storage_key}/v{doc_version}/cleaned.json`，`IngestionService._artifact_key`），
  帶 `doc_version` 的理由也一樣——re-ingest 不互相踩。
- **統計有現成的欄位**：`EtlJob.stats` 是 `JSONField`，`extract`／`clean`／`chunk` 三個
  stage 各自往裡面寫統計已經是既有做法。圖片 stage 照做（§3）。
- **段落追得回原圖**：`Chunk.meta.image_key` 直接存那個 key。

建一張 `document_images` 表能多給的只有「逐張圖的查詢介面」，而 spec 沒有任何需求要求
逐張查詢圖片。**沒有需求的表不建**（憲章的 Model 保持薄、YAGNI）。

> **若日後出現「列出這份文件的所有圖」這種需求，那時再建表**——屆時 key 是決定性的，
> 回填一張表不需要重新處理任何圖片。

---

## 2. 新增與修改的型別（`etl/`，純資料，不進資料庫）

### 2.1 `ExtractedImage`（新，`backend/etl/extract/model.py`）

```
ExtractedImage（frozen=True, slots=True）
├── seq: int              # 在這份文件中的序，從 0 起；決定 storage key
├── page: int | None      # 所在頁；掃描頁點陣化時等於該頁
├── order: int            # 在 blocks 序列中的插入位置（維持前後文順序）
├── heading_path: tuple[str, ...]
├── media_type: str       # "image/png" | "image/jpeg" …
├── content: bytes | None # 影像位元組；因上限而略過時為 None
└── skipped_reason: str | None  # None | "too_large" | "over_document_limit" | "decode_failed"
```

**驗證規則**：

- `content is None` ⇔ `skipped_reason is not None`（兩者必須同時成立，否則是程式錯誤）
- `len(content) <= 單張上限`（R-12 起始值 8 MiB）——**上限在 loader 內就生效**，不是抽完
  再丟；否則沙箱的 1 GiB `RLIMIT_AS` 會先炸
- 一份文件的 `ExtractedImage` 數量 ≤ 單份上限（R-12 起始值 100）；超過的仍要產出項目
  （帶 `skipped_reason="over_document_limit"`），因為 FR-015 要求略過數看得見

### 2.2 `ExtractedDoc`（修改）

```
ExtractedDoc（既有）
├── blocks: tuple[Block, ...]        # 不變
├── doc_meta: dict[str, Any]         # 不變（開放字典）
└── images: tuple[ExtractedImage, ...]   # ← 新增，預設空 tuple
```

**向後相容**：`images` 帶預設值，既有四個 loader（docx／xlsx／text／markdown）一行都不用
改。`etl/artifacts.py` 的 JSON 序列化 `FORMAT_VERSION` 由 `1` 升為 `2`——**但影像 bytes
不進那份 JSON**（見 §3 的落地順序：圖片在中間產物落地之前就已經寫進物件儲存，產物裡留的
是 key 而不是內容）。

### 2.3 `BlockType.CAPTION`（既有，本次首次真的產出）

`BlockType` 早就宣告了 `CAPTION = "caption"`，註解寫著「已宣告但目前無 loader 產出」。
由圖產生的文字就是這一種 block。它的 `BlockMeta` 沿用既有三欄（`order`／`page`／
`heading_path`），因此**切塊、清洗、頁碼與標題階層全部免費沿用**。

---

## 3. 既有資料的擴充（無 migration）

### 3.1 `Chunk.meta`

```
Chunk.meta（JSONField，既有兩鍵）
├── page: int | None                 # 既有
├── heading_path: list[str]          # 既有
├── generated: bool                  # ← 新增；缺鍵視為 false
└── image_key: str | None            # ← 新增；缺鍵視為 null
```

**缺鍵的解讀必須是「否」而不是「不知道」**：既有的每一列 chunk 都沒有這兩個鍵，而它們
全部都是文件原文——把缺鍵讀成 `false`／`null` 是正確的，不是一個將就的預設值。

### 3.2 `EtlJob`

- `stage` 新增一個值 `"image"`。**不需要 migration**：`stage` 是 `TextField`，沒有 choices、
  沒有 CheckConstraint。
- 冪等鍵 `UniqueConstraint(document, doc_version, stage)` 原樣涵蓋新 stage。
- `stats`（stage=`"image"`）的形狀：

```json
{
  "total_images": 12,
  "ocr_succeeded": 11,
  "described": 10,
  "failed": 1,
  "skipped_over_limit": 0,
  "skipped_too_large": 1
}
```

這六個數字合起來就是 FR-023 要的統計，且 `skipped_*` 兩項滿足 FR-015。

### 3.3 `UsageLog`

- `category` 新增一個值 `"vision"`（既有：`"llm"`、`"embedding"`）。`category` 是
  `TextField`，**不需要 migration**。
- `cost` 對地端模型會是 `None`——那是「沒有價目」而不是「免費」（`compute_cost` 查不到
  就回 `None`，見 research R-07）。
- `request_id` = `caption:{document_id}:v{doc_version}:{image_seq}`。

---

## 4. 新表：`KbReextractJob`

**Django app**：`apps/knowledge`。**形狀刻意對齊 `KbReindexJob`**（2B-6），但**資料分開**
（理由見 research R-10）。

```
KbReextractJob(TimestampedModel)
├── id: UUID (pk)
├── tenant: FK(Tenant, PROTECT)            # related_name="kb_reextract_jobs"
├── kb: FK(KnowledgeBase, PROTECT)         # related_name="reextract_jobs"
├── status: TextField = "pending"          # pending | running | completed | failed
├── total_documents: int = 0
├── processed_documents: int = 0
├── cursor: UUID | None                    # 依文件 id 排序的推進游標
├── started_at: datetime | None
├── finished_at: datetime | None
└── error: JSONField | None
```

**約束**：

```
UniqueConstraint(
    fields=["kb"],
    condition=Q(status__in=["pending", "running"]),
    name="uq_reextract_job_active_per_kb",
)
```

逐字沿用 `uq_reindex_job_active_per_kb` 的做法：同一個 KB 同時只能有一個進行中的重跑。
**這是 DB 層的擋，不是先查再建**——併發觸發時第二筆由 `IntegrityError` 轉 `ConflictError`。

**索引**：`(tenant, kb, -created_at)`、`(tenant, status)`——同 `KbReindexJob` 的兩條。

**用游標而不是偏移量**：`cursor` 存「已處理到哪一份文件的 id」。用「已處理筆數」當偏移量
的話，重跑期間有文件被刪或新增就會跳過或重複——`KbReindexJob.rechunk_cursor` 的 docstring
已經記過這件事，此處是同一個理由。

### 4.1 狀態轉移

```
pending ──► running ──► completed
   │           │
   └───────────┴──────► failed
```

- `pending → running`：Celery task 第一次推進時
- `running → running`：每一輪處理一批文件、更新 `cursor` 與 `processed_documents`
- `running → completed`：`cursor` 走完該 KB 的所有文件
- `* → failed`：不可重試的錯誤；`error` 記結構化原因

**這個 job 自己不處理文件**：它只是逐份呼叫既有的 `DocumentService.reingest`
（`ready → parsing`、`doc_version + 1`、舊 chunk 標 `superseded`）。實際的抽取由既有的
`ingest_document` task 做——所以「重跑期間檢索不空窗」不是本表要保證的事，是 `superseded`
的既有語意給的。

### 4.2 Migration

一份 `apps/knowledge/migrations/00XX_kb_reextract_job.py`，只有 `CreateModel`。

**不涉及三步走**（憲章原則 V 的「加欄位帶 default → backfill → 加約束」）：那條規則管的是
**既有表的欄位變更**；新表沒有既有資料，一步到位。**也不需要 `AddIndexConcurrently`**：
新建的空表上建索引不會鎖到任何流量。

---

## 5. 檢索與引用鏈上的型別擴充

### 5.1 `RetrievedChunk`（`backend/rag/retrievers/vector.py`）

```
RetrievedChunk（既有八欄）
├── chunk_id / document_id / content / score
├── page: int | None                 # 由 meta 攤平（既有）
├── heading_path: list[str]          # 由 meta 攤平（既有）
├── document_name / doc_version
├── generated: bool = False          # ← 新增，由 meta 攤平
└── image_key: str | None = None     # ← 新增，由 meta 攤平
```

**這一步不能省**：`RetrievedChunk` **不帶** chunk 的 `meta` dict——`to_retrieved()` 在建構
時就把 `page` 與 `heading_path` 攤平掉了，之後整條鏈（citation、context、SSE、前端）沒有
任何通用的 `meta`／`extra` 欄位可以搭便車。要多帶什麼就得在那裡多攤平一個。

`fuse_candidates()` 用 `dataclasses.replace` 只換 `score`，其餘欄位原樣保留——新欄位因此
自動穿過融合這一關，不需要改它。

### 5.2 `Citation`（`backend/rag/citation.py`）

```
Citation（既有九欄，as_dict() 的鍵即 09 §3.2 契約）
├── marker / chunk_id / doc_id / doc_name / doc_version
├── page / heading_path / score / snippet
├── generated: bool = False          # ← 新增
└── image: ImageRef | None = None    # ← 新增

ImageRef（新，進 as_dict() 時展開為巢狀物件）
├── document_id: str
└── seq: int
```

**`image` 裡沒有 URL，這是刻意的**：`messages.citations` 是**會被永久保存的 jsonb 欄位**，
把短效授權字串寫進去等於讓每一則歷史訊息都夾帶一段當時有效的授權。前端要看圖時另打一支
端點換連結（見 [contracts/api.md](./contracts/api.md)）。

**`document_id` + `seq` 足以定位**：原圖的 key 是決定性的（§1），而 `doc_version` 已經在
`Citation` 既有欄位裡。

### 5.3 `ContextChunk`（`backend/ai/prompts/__init__.py`）——**不變**

送進 LLM 的 context 仍是純文字（`build_context_block()` 產出字串）。FR-012 要求原圖不隨
一般檢索附加——**維持現狀就是滿足它**，本 Feature 在這裡不改任何東西。

> US6（追問時把原圖交給模型）才會動到這條路徑，而那需要 `ChatMessage.content` 由 `str`
> 改成多模態（research R-05 的第二段）。**它與上面所有內容互不相依。**

---

## 6. 資料生命週期

| 事件 | 圖片物件的處置 |
|------|----------------|
| re-ingest（`doc_version + 1`） | 新版本寫到新的 `v{n+1}/images/` 前綴下；舊版本的物件留著，由保留窗清理 |
| 文件軟刪除 | 不立即刪物件（同既有做法：軟刪除只標記，chunk 標 `superseded`） |
| 保留窗到期的硬刪 | 由既有的 `DeletedKnowledgePurgeService` 以 `{storage_key}/` **前綴**刪除，一併涵蓋圖片與 `cleaned.json` |
| KB 刪除 | 同上，經既有的 KB 級清理路徑 |

**一個需要確認、但本 Feature 不修的既有情況**：`{storage_key}/v{n}/cleaned.json` 這個中間
產物目前是否真的會被清掉，我在盤點時沒有確認到。若它一直殘留，那是一個**先於本 Feature 就
存在**的問題——依任務卡規則列入回報清單，由人決定是否另開任務，不在本包順手改。

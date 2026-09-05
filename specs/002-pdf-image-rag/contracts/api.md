# Contract: HTTP API 與 SSE 事件

本 Feature 對外的介面有三處變動：**兩支新端點**、**一個既有 SSE 事件的相容擴充**、
以及**一個既有端點的行為改變（沒有形狀變動）**。

> **憲章原則 V**：新增端點必須同步權限 code、`operation_id`（命名穩定性視同 API 契約）、
> 審計事件（若屬敏感操作），並跑 `make openapi && make gen-api` **兩段**。

---

## 1. `GET /documents/{document_id}/images/{seq}/url`（新增）

換一條看原圖的短效連結。FR-009／FR-010／FR-021。

| 項目 | 值 |
|------|-----|
| `operation_id` | `documents_image_url` |
| 權限 code | `knowledge:read` |
| 審計 | **無**（GET 不觸發 `AuditMiddleware`，它只認 `POST/PUT/PATCH/DELETE`） |
| 成功 | `200` + `ImageUrlOut` |

**Path 參數**：`document_id: UUID`、`seq: int ≥ 0`。

**Query 參數**：`doc_version: int | None` — 省略時用該文件的當前版本。**歷史訊息裡的引用
需要它**：那則引用指的是當時那一版的圖，而文件可能已經 re-ingest 過。

**回應**：

```json
{
  "url": "https://…?X-Amz-Expires=300&…",
  "expires_at": "2026-09-05T12:34:56Z"
}
```

**錯誤**：

| 情況 | 回應 |
|------|------|
| 文件不存在、已軟刪除、或不屬於當前租戶 | `404`（**不是 403**——存在與否本身就是資訊） |
| 該 `seq` 的圖不存在（含因上限而略過的） | `404` |
| 呼叫者無 `knowledge:read` | `403` |

**兩層檢查各在各的位置**（research R-08）：

1. **業務授權在 service**：這個使用者的租戶能不能讀這份文件。
2. **key 歸屬在 core**：`core/object_storage.py` 既有的 `_require_own_key` 會比對
   `tenant-{tenant_id}/` 前綴，不符即 `CrossTenantObjectKeyError`。短效連結沿用同一道。

**為什麼回 JSON 而不是 302 轉址**：轉址會讓 codegen 產不出可用的型別，前端得自己處理
重導；而且 `expires_at` 是前端決定「要不要重新換一條」的依據，轉址沒有地方放它。

---

## 2. `POST` / `GET /knowledge-bases/{kb_id}/reextract`（新增）

逐 KB 觸發重新抽取，讓既有文件回頭補上圖片。FR-018／FR-019／FR-020。

### 2.1 `POST`（觸發）

| 項目 | 值 |
|------|-----|
| `operation_id` | `knowledge_bases_reextract` |
| 權限 code | `knowledge:admin` |
| 審計 | `AuditSpec("knowledge_base.reextract", "knowledge_base", "kb_id")` |
| 成功 | `202` + `KbReextractJobOut` |

**Request**：無 body（整個 KB 一律全做）。

**錯誤**：

| 情況 | 回應 |
|------|------|
| 該 KB 已有進行中的重新抽取 | `409`（由 DB 的 partial unique 擋，非先查再建） |
| 該 KB 已有進行中的 **reindex** | `409` — **兩者不得同時跑**。理由與 2B-6 的「重切與換模型不得同一個 job」同一條：重新抽取會產生全新的 chunk 列，而 reindex 正在對舊的那批算向量 |
| KB 不存在／不屬於當前租戶 | `404` |
| 無 `knowledge:admin` | `403` |

### 2.2 `GET`（查進度）

| 項目 | 值 |
|------|-----|
| `operation_id` | `knowledge_bases_reextract_status` |
| 權限 code | `knowledge:read` |
| 成功 | `200` + `KbReextractJobOut`；**沒有任何紀錄回 `404`** |

回 404 而不是 `null`，是沿用 `knowledge_bases_reindex_status` 的既有做法。

### 2.3 `KbReextractJobOut`

```
id: UUID
kb_id: UUID
status: str            # pending | running | completed | failed
total_documents: int
processed_documents: int
started_at: datetime | None
finished_at: datetime | None
error: dict[str, Any] | None
```

**刻意不含 `cursor`**：它是內部推進狀態，不是使用者要看的東西。（`KbReindexJobOut` 同樣
不帶 `rechunk_cursor`。）

---

## 3. `citations` SSE 事件與 `messages.citations`（相容擴充）

09 §3.2 的既有九個欄位**一個都不動**，新增兩個：

```
event: citations
data: {"items": [{
    "marker", "chunk_id", "doc_id", "doc_name", "doc_version",
    "page", "heading_path", "score", "snippet",

    "generated": false,                                  ← 新增
    "image": null | {"document_id": "…", "seq": 0}       ← 新增
}]}
```

| 欄位 | 語意 |
|------|------|
| `generated` | 這一段是機器看圖產生的描述，不是文件原文（FR-008）。缺鍵讀成 `false` |
| `image` | 這一段來自哪一張圖；`null` 表示不是由圖產生的（FR-009 的入口） |

**`image` 裡沒有 URL，這是刻意的**：`messages.citations` 是永久保存的 jsonb 欄位，把短效
授權字串寫進去等於讓每一則歷史訊息夾帶一段當時有效的授權。要看圖時打 §1 換。

**契約影響比想像中小**：`MessageOut.citations` 的型別是 `list[dict[str, Any]]`（jsonb 原樣
透傳，沒有逐鍵的 pydantic 模型），codegen 產出的 TS 是 `{[key: string]: unknown}[]`。
**所以這個擴充本身不會讓 `openapi.json` 產生任何漂移**——會漂移的是 §1／§2 的新端點。

**前端要手改**（不是 codegen 產物）：`frontend/src/utils/citations.ts` 的 `CitationItem`
是手寫 interface，`CitationPanel.vue` 是實際渲染處。

---

## 4. `POST /documents`（既有端點，形狀不變、行為改變）

**沒有任何 request／response 欄位變動。** 改變的是掃描件的結局：

| | 本 Feature 之前 | 之後 |
|---|---|---|
| 上傳沒有文字層的 PDF | `202` 收下，ETL 在 extract 階段失敗，文件狀態 `failed`，`error` 訊息為「PDF 沒有可抽取的文字層（掃描件需 OCR，目前未啟用）」 | `202` 收下，OCR 產出可檢索文字，文件狀態 `ready` |

**MIME 白名單一個字都不動**（FR-007）：`ACCEPTED_MEDIA_TYPES` 維持五種，`MAX_UPLOAD_BYTES`
維持 32 MiB，配額的計算方式不變。

**一項要保住的既有防線**：`etl/extract/loaders/pdf.py` 目前對「沒有文字層」的處置是**明確
失敗**而不是回空文件，理由寫在該檔開頭——回空的話文件會走完整條 ETL 停在 `ready` 而一個
chunk 都沒有，使用者只會覺得「這份好像沒被讀進去」。本 Feature 把這條路打開之後，那個
判斷**必須改成「OCR 也沒有產出任何文字才失敗」，不能直接刪掉**（spec US1 情境 3）。

---

## 5. 不變動的既有端點（列出以界定範圍）

| 端點 | 本次 |
|------|------|
| `POST /knowledge-bases/{id}/reindex` | **不動**。重新抽取是另一件事，另一支端點、另一張表（research R-10） |
| `POST /documents/{id}/reingest` | **不動**。逐 KB 的重新抽取在內部逐份呼叫它 |
| `GET /conversations/{id}/messages/{mid}/stream` | 形狀不動，只有 `citations` 事件多兩個欄位（§3） |
| 所有 `/analytics/*` | **不動**。`category="vision"` 是 `UsageLog` 的新值，日彙總的形狀不變 |

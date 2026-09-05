# Phase 1 Data Model: 換 embedding 模型後的檢索品質重新定錨

**Date**: 2026-09-05 | **Spec**: [spec.md](./spec.md) | **Research**: [research.md](./research.md)

本 Feature **不新增也不修改任何資料庫表**。異動集中在三種離線資料檔的形狀上：題組、
評測報告、以及比較的產物。下表只列**變動的部分**；未列出的欄位一律維持既有語意。

---

## 1. Question（題組的一列，JSONL）

`backend/evaluation/goldenset/handwritten.jsonl`，解析在 `backend/rag/goldenset.py`。

| 欄位 | 型別 | 本次變動 |
|------|------|----------|
| `question_id` | str | 續編 `hw-25`…`hw-50`；既有 `hw-01`…`hw-24` **不得改動** |
| `question` | str | 新題由 AI 起草、人類逐題改寫（FR-002／FR-002a） |
| `passage_ids` | list[str] | 必須指向 `corpus/lumina_docs.jsonl` 內既有的段落（語料不變，FR-005） |
| `language` | str | 白名單不變：`zh-Hant`／`en` |
| `source` | str | **新增允許值**（如 `ai-drafted`），與既有的 `handwritten` 並存 → FR-002b 的可區分性 |

**不變量**
- 題數 ≥ 50（FR-001），跨語言（`en` 問句 → 中文段落）≥ 3 題
- `question_id` 在兩份題組之間全域唯一
- 每個 `passage_ids` 的元素都能在語料中找到
- 檔案指紋 `sha256` 會因本次擴充而改變 → 既有 handwritten baseline 失效（設計如此）

---

## 2. Report（一次評測的產物，JSON）

`backend/evaluation/reports/<model_slug>/<mode>_<dataset>.json`，產生在
`backend/scripts/eval_retrieval.py::build_report`。

| 區塊 | 欄位 | 本次變動 |
|------|------|----------|
| 頂層 | `schema_version` | **2 → 3**（新增必填欄位，讀報告的人要分得出「這份沒有維度」是舊的還是跑掉了） |
| `dataset` | `name` / `goldenset_sha256` / `corpus_sha256` / `question_count` / `passage_count` | 不變 |
| `retrieval` | `embedding_provider` / `embedding_model` | 不變 |
| `retrieval` | **`embedding_dimensions`** | **新增，必填**。取自實際存下來的向量長度，不是設定值（R-03） |
| `retrieval` | `top_k` / `mode` / `params` | 不變 |
| `retrieval` | `rerank_provider` / `rerank_model` | 不變（僅 rerank 模式，既有強制規則不動） |
| `metrics` | — | 不變（`recall@{1,5,10,20}`、`hit@…`、`mrr`、`question_count`） |
| `rerank_scores` | — | 不變 |
| `per_question` | — | 不變 |

**不變量**
- `embedding_dimensions` 缺漏時，該報告視為**不可比**，而非「維度相同」（FR-008）
- rerank 模式仍必須帶 `rerank_provider` 與 `rerank_model`，否則拒絕產出（FR-009）

---

## 3. Comparison（兩份報告的差異）

`backend/scripts/eval_retrieval.py::Comparison`（frozen dataclass）。

| 欄位 | 本次變動 |
|------|----------|
| `primary` / `secondary` / `baseline` / `candidate` / `secondary_baseline` / `secondary_candidate` / `deltas` / `improved` | 不變——2B 系列的每一個結論都建立在 `improved` 的既有語意上 |
| **`cross_model`** | **新增**：本次比較是否為顯式的跨模型比較 |
| **`baseline_embedding`** / **`candidate_embedding`** | **新增**：各自的 (provider, model, dimensions) 三元組 → FR-011 |

**可比性規則的變動**（`_require_comparable`）

| 比對項 | 既有 | 本次 |
|--------|------|------|
| `dataset.name` / `goldenset_sha256` / `corpus_sha256` | 必須相同 | **不變**——跨模型旗標**不放寬**這三項（FR-012） |
| `retrieval.embedding_model` | 必須相同 | 預設必須相同；`cross_model=True` 時放寬 |
| `retrieval.embedding_dimensions` | （不存在） | **新增**：預設必須相同；`cross_model=True` 時放寬；任一邊缺漏即拒絕 |

---

## 4. Verdict（三檔判定，本次新增）

純函式的回傳值，不落檔為獨立檔案——它由已提交的報告重算得出（SC-008）。

| 欄位 | 內容 |
|------|------|
| `level` | `優於` / `持平` / `劣於` |
| `decided_by` | 裁決依據：手寫題組的 `hybrid+rerank`（FR-020） |
| `primary_delta` | 裁決題組的主指標差值 |
| `guard_delta` | 公開題組的主指標差值（迴歸護欄） |
| `guard_vetoed` | 護欄是否否決（FR-022） |
| `reason` | 人可讀的一句話，說明落在這一檔的原因 |

**判定規則**（門檻來自 spec Assumptions，非本檔決定）

```
若 公開題組主指標退步 > 0.83pp  →  劣於（護欄否決，優先於下方任何結果）
否則 若 |手寫題組主指標差| ≤ 2pp →  持平
否則 若 手寫題組主指標差 > 0     →  優於
否則                             →  劣於
```

**不變量**
- 判定只讀報告，不讀環境、不讀任何人的記憶（SC-008）
- 四個模式中只有 `hybrid+rerank` 參與裁決；其餘三個供歸因（FR-020）

---

## 5. 不動的東西（明列，避免實作時漂移）

- **資料庫**：無新表、無欄位變更、無 migration（FR-024 亦要求回退不得需要 schema 變更）
- **語料**：`corpus/drcd.jsonl`、`corpus/lumina_docs.jsonl` 位元組不變（FR-005）
- **公開題組**：`goldenset/drcd.jsonl` 不變
- **指標定義**：`rag/metrics.py` 不動；`KS`、`PRIMARY_METRIC`、`SECONDARY_METRIC` 不動
- **四個模式的語意**：`MODES` 不動
- **檢索參數**：不因結論調整任何預設值（FR-025）

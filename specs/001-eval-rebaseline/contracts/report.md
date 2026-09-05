# Contract: 評測報告 JSON

報告是本 Feature 唯一的持久化產物，也是 spec 中「結論可由任何人重算」（SC-008）與
「不查外部紀錄就能指認出當時的設定」（SC-005）唯一的載體。

**Schema version：2 → 3**（新增必填欄位 `retrieval.embedding_dimensions`）。

---

## 形狀（僅列與既有版本的差異）

```jsonc
{
  "schema_version": 3,                     // 2 → 3
  "mode": "hybrid+rerank",
  "created_at": "2026-09-05T…+00:00",
  "dataset": {
    "name": "handwritten",
    "goldenset_sha256": "…",               // 手寫題組擴充後會改變
    "corpus_sha256": "…",                  // 不變（FR-005）
    "question_count": 50,                  // 24 → ≥50
    "passage_count": 299
  },
  "retrieval": {
    "embedding_provider": "tei",
    "embedding_model": "BAAI/bge-m3",
    "embedding_dimensions": 1024,          // ★ 新增，必填，取自實際向量長度（R-03）
    "top_k": 40,
    "mode": "hybrid+rerank",
    "params": { … },
    "rerank_provider": "tei",              // 僅 rerank 模式，既有規則不變
    "rerank_model": "BAAI/bge-reranker-v2-m3"
  },
  "metrics": { … },                        // 不變
  "rerank_scores": { … },                  // 不變，僅 rerank 模式
  "per_question": [ … ]                    // 不變
}
```

---

## 不變量

| # | 規則 | 對應 |
|---|------|------|
| 1 | `retrieval.embedding_dimensions` 必填，且必須是**實際存下來的向量長度** | FR-006 / R-03 |
| 2 | 缺少該欄位的報告一律視為**不可比**，不得推定為維度相同 | FR-008 |
| 3 | rerank 模式缺 `rerank_provider`／`rerank_model` 時**拒絕產出報告** | FR-009（既有） |
| 4 | 報告落在 `reports/<model_slug>/<mode>_<dataset>.json`，路徑本身即可指認模型、模式、題組 | FR-015 / R-05 |
| 5 | `dataset.*` 三個指紋欄位在任何比較中都必須相同，跨模型旗標**不放寬** | FR-012 |

## 版本相容

- version 2 的既有報告**仍可被讀取**，但因缺 `embedding_dimensions` 而在任何比較中被拒絕
  （不變量 2）。這是刻意的：它們是 1536 維量出來的，本來就不該與新報告相減。
- 舊報告移入 `reports/legacy-gemini-1536/` 並在 `evaluation/README.md` 標示為歷史數據
  （FR-016）。**不刪除**——它們是 2B 系列結論的證據。

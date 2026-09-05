# Contract: 評測 CLI 與 Make 目標

本 Feature 對外的介面只有兩種：**人手打的指令**，與**它產出的 JSON 報告**（見
[report.md](./report.md)）。沒有 HTTP 端點、沒有 OpenAPI 變更、沒有前端。

> **既有界線不變**：這些指令一律手動執行、會打真 API，**不得**被接進 `make test`、
> `make lint`、`make smoke` 或 CI。守門測試 `tests/unit/test_eval_runner.py::TestItStaysOutOfTheAutomatedSuites`
> 必須維持有效（FR-017）。

---

## 1. `make eval-retrieval`（既有，本次擴充）

```
make eval-retrieval DATASET=<drcd|handwritten> MODE=<vector|vector+rerank|hybrid|hybrid+rerank> \
                    [EVAL_ENV=<env 檔路徑>] [EVAL_ARGS="…"]
```

| 參數 | 變動 | 說明 |
|------|------|------|
| `DATASET` / `MODE` / `EVAL_TENANT` | 不變 | |
| **`EVAL_ENV`** | **新增**，預設 `../.env` | 指定這次評測用哪一份環境設定，用來在兩個 embedding 模型之間切換（R-06） |
| `EVAL_ARGS` | 不變 | 透傳給腳本的旗標 |

**輸出路徑變動**：預設由 `reports/<mode>_<dataset>.json` 改為
`reports/<model_slug>/<mode>_<dataset>.json`（R-05）。`--out` 仍可覆寫。

**既有行為維持**：`--baseline` 跑完順便比對；mock 守門（embedding 為 mock、或 rerank
模式而 reranker 為 mock）在加 `--allow-mock` 之前一律擋下。

---

## 2. `make eval-compare`（本次新增）

```
make eval-compare BASE=<報告路徑> CAND=<報告路徑> [EVAL_ARGS="--cross-model"]
```

**只比不跑**：讀兩份既有報告、輸出差異與判定，**不碰資料庫、不打任何外部服務**。
這是 spec US2 能列為「可獨立驗證」的前提——驗可比性規則不該需要 GPU 與金鑰（R-04）。

| 行為 | 預期 |
|------|------|
| 題組或語料指紋不同 | 拒絕，列出不同的欄位（**`--cross-model` 也不放寬**，FR-012） |
| embedding 模型不同，未加 `--cross-model` | 拒絕，訊息指名 `retrieval.embedding_model`（FR-010，US1 情境 1） |
| embedding 維度不同，未加 `--cross-model` | 拒絕，訊息指名 `retrieval.embedding_dimensions`（FR-007，US2 情境 2） |
| 任一邊缺 `embedding_dimensions` | 拒絕（**不得**視為維度相同，FR-008，US2 情境 3） |
| 加了 `--cross-model` 且指紋相同 | 輸出各指標差值與勝負判定，並**同時列出兩邊的 provider／model／dimensions**（FR-011，US1 情境 2） |

**離場碼**：可比且完成比較為 `0`；拒絕比較為非 `0`（沿用既有 `--baseline` 的行為，
讓誤用在腳本裡也是失敗而不是一段被忽略的輸出）。

---

## 3. `make eval-verdict`（本次新增）

```
make eval-verdict HANDWRITTEN_BASE=… HANDWRITTEN_CAND=… DRCD_BASE=… DRCD_CAND=…
```

輸入四份報告（兩份題組 × 兩個模型的 `hybrid+rerank`），輸出三檔判定與理由。
**同樣只讀報告**，因此判定可由任何人以已提交的報告重算（SC-008）。

| 行為 | 預期 |
|------|------|
| 公開題組主指標退步 > 0.83pp | `劣於`，且 `guard_vetoed=true`——**護欄的否決優先於手寫題組的任何結果**（FR-022，US4 情境 2） |
| 手寫題組主指標差 ≤ 2pp | `持平`（FR-021，US4 情境 1） |
| 手寫題組主指標差 > +2pp 且護欄未否決 | `優於` |
| 手寫題組主指標差 < −2pp | `劣於` |

判定**不執行任何動作**——切不切換模型是人依這個結論做的下一步（見 quickstart）。

---

## 4. 既有指令（本次不改，列出以界定範圍）

| 指令 | 用途 | 本次 |
|------|------|------|
| `make eval-sample SOURCE=…` | 重新取樣語料 | **不跑**——語料必須不變（FR-005） |
| `make eval-clean` | 清掉評測租戶的 `eval-*` 知識庫 | **不跑**——會連 chunk 一起刪，本次要保留它們 |
| `make demo-tenant DEMO_SLUG=lumina-eval` | 開通評測租戶 | 已存在，不需重跑 |
| `make tei-up` / `make tei-embed-up` | 起 rerank / embedding 的 GPU 容器 | **要跑**，兩個都要 |
| `make verify-provider PROVIDER=… CAPABILITY=embedding` | 實測某家 provider | **要跑**（R-08 驗正規化假設） |

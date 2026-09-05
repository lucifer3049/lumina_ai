# Quickstart: 驗證這個 Feature 真的做到了

**Spec**: [spec.md](./spec.md) ｜ **Contracts**: [cli.md](./contracts/cli.md)、[report.md](./contracts/report.md)

分兩段：**A 段不需要 GPU、金鑰或資料庫**（可比性規則與判定邏輯），**B 段是 16 次真實
評測**（要 GPU、金鑰、約數十分鐘）。A 段就能驗完 spec 的 US2、US3 與 US4 的判定規則；
B 段驗 US1 與 US4 的實際結果。

---

## 前置

```bash
make up                                  # 基礎服務（postgres / redis / minio）
make tei-up                              # rerank 容器（GPU，8080）
make tei-embed-up                        # embedding 容器（GPU，8081；首次啟動要等模型下載）
```

> 兩個 GPU 容器與地端 embedding 的啟用已於 2026-09-05 完成並實測（R-10），
> 這裡列出指令是為了「重開機之後要做什麼」，不是還沒做的事。
>
> **⚠️ 2026-09-06 更新：評測租戶與兩個評測知識庫都不存在了。** Docker 由 Desktop 改為
> WSL2 內的原生 Engine（13 §4.3）時舊資料卷不遷移，`lumina-eval` 租戶、`eval-drcd`、
> `eval-handwritten` 與那 1,499 個 chunk 隨之消失（實查：DB 內只剩 smoke 殘留）。
> 因此 B 段開跑前**必須**先開通租戶：
>
> ```bash
> make demo-tenant DEMO_SLUG=lumina-eval
> ```
>
> 語料**不必**手動重灌——`eval-retrieval` 會自行建立知識庫、灌語料、算向量（見 R-02）。
> 第一次跑會因此比之前久。**本節原文寫的是「租戶已存在，不要跑 `make demo-tenant`」，
> 那句話現在會把人擋在唯一的解法外面**：`resolve_kb` 找不到租戶時的錯誤訊息正好叫你跑
> 這個指令。
>
> 仍然成立：**不要**跑 `make eval-sample`（會改語料指紋）；`make eval-clean` 現在沒有
> 東西可刪，但語意不變——**不要**跑它。

先驗一件事——spec Assumptions 裡唯一沒被證實的那條（R-08）：

```bash
make verify-provider PROVIDER=gemini CAPABILITY=embedding
```
**預期**：回報維度 `1024`、單位長度 `1.000000`。**若不是單位長度，停下回報**——
相似度計算的前提不成立，後面 16 次評測都不必跑。

---

## A 段：不碰外部服務的驗收

### A1 可比性規則（US2）

```bash
# 模型不同、未加旗標 → 必須拒絕
make eval-compare BASE=backend/evaluation/reports/gemini-embedding-2/vector_handwritten.json \
                  CAND=backend/evaluation/reports/bge-m3/vector_handwritten.json
```
**預期**：非 0 離場碼，訊息指名 `retrieval.embedding_model`。

```bash
# 同一組報告、加上旗標 → 必須比得出來，且列出兩邊的模型與維度
make eval-compare BASE=… CAND=… EVAL_ARGS="--cross-model"
```
**預期**：離場碼 0；輸出含兩邊的 provider／model／dimensions 三元組與各指標差值。

```bash
# 舊的 1536 維報告（schema v2，無維度欄位）→ 即使加旗標也拒絕
make eval-compare BASE=backend/evaluation/reports/legacy-gemini-1536/baseline_vector_handwritten.json \
                  CAND=… EVAL_ARGS="--cross-model"
```
**預期**：拒絕。缺欄位不得被推定為「維度相同」，且題組指紋也已不同（題組擴充過）。

### A2 題組（US3）

```bash
make test-file FILE=tests/unit/test_golden_set.py
```
**預期**：全綠，且題數斷言的下限已是 50、跨語言題 ≥3、新舊題的 `source` 可區分。

```bash
make test-file FILE=tests/unit/test_eval_runner.py
```
**預期**：在 B 段跑完之前，`test_the_baseline_still_matches_the_dataset_in_the_repo`
的 handwritten 那一條**應該是紅的**——題組改了而 baseline 還沒重跑。這條紅是設計，
不是缺陷；它在 B 段結束、新 baseline 落檔後才轉綠。

### A3 三檔判定（US4）

```bash
make test-k K=verdict
```
**預期**：全綠。涵蓋護欄否決優先於手寫題組結果、差距 ≤2pp 判持平、以及判定只讀報告。

---

## B 段：16 次真實評測（US1）

兩份 env 檔（不進版控）各自完整可用：

```
.env.eval-tei      → AI_EMBEDDING_PROVIDER=tei     AI_EMBEDDING_MODEL=BAAI/bge-m3
                     AI_EMBEDDING_BASE_URL=http://127.0.0.1:18081/v1
.env.eval-gemini   → AI_EMBEDDING_PROVIDER=gemini  AI_EMBEDDING_MODEL=gemini-embedding-2
                     AI_EMBEDDING_API_KEY=…（不要留 AI_EMBEDDING_BASE_URL，否則雲端請求會打到本機容器）
```
兩份都要有 `AI_RERANK_PROVIDER=tei` 與 `AI_RERANK_MODEL=BAAI/bge-reranker-v2-m3`。

```bash
for ENV in .env.eval-tei .env.eval-gemini; do
  for DS in drcd handwritten; do
    for MODE in vector vector+rerank hybrid hybrid+rerank; do
      make eval-retrieval EVAL_ENV=../$ENV DATASET=$DS MODE="$MODE"
    done
  done
done
```

**每一次的預期**：
- 摘要行印出的 provider／model **與該次的 env 檔一致**（切換沒生效時兩個模型會印出相同
  的名字——那是最先看得見的徵兆）
- 報告落在 `reports/<model_slug>/<mode>_<dataset>.json`
- 報告內 `retrieval.embedding_dimensions` 為 `1024`

**第一次跑的額外預期**：會重新嵌入（`eval-drcd` 1200 段、`eval-handwritten` 299 段）——
W1 的 migration 清空過向量。之後同一個模型的其餘三個模式不會再嵌一次。

### B1 產出對照表與判定

```bash
make eval-verdict HANDWRITTEN_BASE=…/gemini-embedding-2/hybrid_rerank_handwritten.json \
                  HANDWRITTEN_CAND=…/bge-m3/hybrid_rerank_handwritten.json \
                  DRCD_BASE=…/gemini-embedding-2/hybrid_rerank_drcd.json \
                  DRCD_CAND=…/bge-m3/hybrid_rerank_drcd.json
```
**預期**：輸出 `優於`／`持平`／`劣於` 其中之一與理由。

### B2 依判定行動

系統目前跑的是**地端模型**（2026-09-05 切換，見 [research.md](./research.md) R-10），
因此：

- **優於／持平** → 不做任何設定變更
- **劣於**（或公開題組否決）→ 依 FR-023 回退到雲端模型、重建向量至檢索可用

無論落在哪一檔，三件事都必須發生：

1. `evaluation/README.md` 更新為 2 模型 × 4 模式 × 2 題組的對照表，每格可追到報告檔
2. `docs/plan/13` §4.2 的 W1 未做項③ 依實測結果結案，並記下判定結論
3. **FR-027**：切換的日期、方向與依據落進上述兩份文件——實際生效的設定不進版控，
   文件是唯一可審查的產物

---

## 收尾（憲章閘門 4，四項缺一不可）

```bash
make lint
make test-unit && make test-integration && make test-api
make smoke
make openapi-check
```

**預期**：全綠。本 Feature 不動 API，`openapi-check` 應無漂移；若有漂移代表改到了不該
改的地方，停下回報。

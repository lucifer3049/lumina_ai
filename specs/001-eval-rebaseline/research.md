# Phase 0 Research: 換 embedding 模型後的檢索品質重新定錨

**Date**: 2026-09-05 | **Spec**: [spec.md](./spec.md)

本檔記錄開工前必須查清、且查完之後就不必再猜的事。每一項都在本機實查過，不是從文件
推得的。**其中 R-10 是一個必須由人類裁決的衝突，plan 不自行解決。**

---

## R-01 兩個 embedding 模型的向量能不能並存在同一個評測知識庫

**Decision**：能，且**不需要任何程式改動**。切換環境變數各跑一次即可。

**Rationale**：兩個評測知識庫的 `embedding_model` 欄位都是空字串（實查），因此
`services/knowledge/embedding.model_for()` 會退回 `AI_EMBEDDING_MODEL` 的值。寫入端的
`_pending_chunks` 以 `model` 過濾「還缺哪些向量」，讀取端的向量搜尋同樣以
`model + embedding_version` 過濾（`repositories/knowledge.py` 的註解明寫「少任何一個，
回來的 chunk 都確實存在且看起來合理」）。兩個模型現在同為 1024 維，`halfvec(1024)` 欄位
兩邊都塞得下。

**Alternatives considered**：為每個模型另開一個知識庫（`eval-drcd-tei` / `eval-drcd-gemini`）。
否決——`resolve_kb` 的命名規則要改、`eval-clean` 的清理範圍要改，換來的隔離已經由
`model` 欄位提供了。

---

## R-02 W1 清空向量之後，重跑評測會不會誤判為「已經灌過了」

**Decision**：不會。**不需要程式改動**，spec 的該條 Edge Case 由既有邏輯覆蓋。

**Rationale**：實查 DB —— `eval-drcd` 1200 chunk / **0** 向量、`eval-handwritten`
299 chunk / **0** 向量，正是 W1 `TRUNCATE` 之後的狀態。`_ensure_document` 的冪等檢查是
「文件存在**且** chunk 數 == 段落數」，1200 == 1200 成立，於是重用該文件；接著
`embed_document` 只算缺向量的 chunk，等於整批重嵌。這條路徑本來就是為了「跑到一半崩潰」
設計的，換維度剛好落在它的守備範圍內。

**風險**：`_ensure_document` 在 chunk 數對不上時**刻意不自動修**，要求人手刪文件重跑。
本次不會走到那條分支（數量對得上），但若擴充題組時誤動了語料就會——語料不動是 FR-005。

**2026-09-06 更新：上面那筆實查已經作廢，而結論不變。** Docker 由 Desktop 改為 WSL2 內的
原生 Engine（13 §4.3）時舊資料卷不遷移，**`lumina-eval` 租戶與兩個評測知識庫整個不存在了**
（實查 DB：只剩 smoke 殘留的 2 個 KB／10 個 chunk）。因此本節推理所依據的「1200 chunk /
0 向量、1200 == 1200 成立、於是重用文件」在今天**一條都不成立**——實際會走的是
`_ensure_document` 的**建立**路徑，連冪等判斷都用不到。

結論（「不需要程式改動」）反而更穩：空庫沒有「誤判為已經灌過了」的機會。但**兩件事變了**：

1. **多一個前置**：`resolve_kb` 找不到租戶會 fail fast，要先 `make demo-tenant
   DEMO_SLUG=lumina-eval`。已回寫 [quickstart.md](./quickstart.md) 的前置段——原文寫的
   「不要跑 `make demo-tenant`」正好擋住唯一的解法。
2. **上面的「風險」那一段暫時沒有受測對象**：chunk 數對不上的分支需要既有文件才走得到，
   而現在沒有既有文件。擴充題組（US3）之後第一次跑 B 段時，它會回到有效狀態。

**原文保留**（沿用本 repo 的慣例）：它記錄的是 2026-09-05 當下的事實與當時的推理，
作廢的是依據不是結論。

---

## R-03 報告裡的「embedding 維度」該從哪裡取

**Decision**：取**實際存下來的向量長度**（從該知識庫該模型的任一列向量讀出），
不是 `AI_EMBEDDING_DIMENSIONS` 的設定值。

**Rationale**：設定值是「請求的維度」，而它不一定送得出去——`VendorSpec.supports_dimensions`
為 False 的廠商（`tei`、`vllm`、`nvidia`）根本不會帶這個參數，設定填 512 也照樣回 1024。
報告要記的是**量到的東西**，不是要求的東西；記設定值等於在報告裡放一個看起來精確而可能
不成立的數字，而那正是 FR-006 這條需求要防的類型。

**Alternatives considered**：記 `settings.ai_embedding_dimensions`（一行就好）。否決，理由
如上。另一個選項是記 gateway 回傳的實際長度——那要把值從灌語料階段傳到產報告階段，跨越
兩次獨立執行（README 明寫「灌一次、之後每個模式各跑一次」），行不通。

---

## R-04 「拿兩份既有報告來比」要用什麼介面

**Decision**：新增一個**只比不跑**的入口（`make eval-compare BASE=… CAND=…`，
必要時加 `--cross-model`），與既有的 `--baseline`（跑完順便比）並存。

**Rationale**：spec 的 US1 情境 1–3 與 US2 情境 2–3 都是「拿兩份報告比」，而現行 CLI 只有
「跑一次評測、跑完與 baseline 比」這一條路——要驗那些情境就得先跑一次真評測，於是需要
GPU、金鑰與 20 分鐘。**只比不跑的入口讓 US2 完全不必碰外部服務**，這也是它能列為
「可獨立驗證」的前提。

**Alternatives considered**：只加 `--cross-model` 旗標給既有的 `--baseline`。否決——那條路
綁著「先跑一次評測」，US2 的驗收會被迫依賴 GPU 與金鑰，違反「評測不得進自動化測試」與
「驗收測試要跑得動」之間本來就很緊的平衡。

---

## R-05 16 份報告的檔名與版控

**Decision**：
- 報告改寫進**每個模型一個子目錄**：`evaluation/reports/<model_slug>/<mode>_<dataset>.json`。
- 既有 8 份 1536 維報告移到 `evaluation/reports/legacy-gemini-1536/`，README 標為歷史。
- `baseline_vector_<dataset>.json` **兩個檔名保持不動**，語意收斂為「系統當前實際使用的
  那個模型的純向量對照組」，內容於本 Feature 結束時依判定結果落定。
- `.gitignore` 放行新的模型子目錄；16 份全部進版控（實測既有 8 份合計 ~967 KB，
  16 份估 ~1.6 MB）。

**Rationale**：FR-015 要求「由檔名指認出模型、模式與題組」，而現行 `_default_out` 只有
`<mode>_<dataset>.json`——兩個模型跑同一個模式會互相覆蓋，而覆蓋沒有任何徵兆。
`baseline_vector_*` 不改名是因為 `tests/unit/test_eval_runner.py::TestBaseline` 以那兩個
檔名為錨；改名要同時改守門測試，而那個守門正是本次唯一會自然變紅的測試（見 R-09），
不該在同一次動作裡把它的錨也換掉。

**實查發現**：`.gitignore` 只放行 `baseline_*.json`，因此 **2B-4 那張四模式表背後的
6 份報告從來沒有進過版控**——數字只活在 `evaluation/README.md` 的表格裡。本次把 16 份
全部提交，順帶把那個缺口補上（對舊報告則是移進 legacy 目錄後一併提交）。

---

## R-06 兩個模型怎麼切換才不會切錯

**Decision**：Makefile 新增 `EVAL_ENV`（預設空），評測目標在既有的 `--env-file ../.env`
之後**再疊一層** `--env-file $(EVAL_ENV)`；每個模型一份未進版控的覆蓋檔
（`.env.eval-tei`／`.env.eval-gemini`，只寫差異的幾行），`.env.example` 補說明。
**疊加而非取代**是 T001 實測之後的定案，見下方「已驗」。

**Rationale**：`UV_RUN := uv run --env-file ../.env`——uv 的 `--env-file` 對**已存在的
環境變數**的覆蓋語意需要實測確認（見下方待驗），因此「在命令列前面塞 `AI_EMBEDDING_PROVIDER=tei`」
不是可靠的做法。用兩份完整的 env 檔則語意明確、事後也看得出當時跑的是哪一組。

**安全網（即使切錯也不會產生錯誤結論）**：報告記的 `embedding_provider` 與
`embedding_model` 都來自實際生效的設定，加上 R-03 的實際維度，於是「切換沒生效」的症狀
是**兩份報告的模型欄位相同**——`_require_comparable` 會當場拒絕比較，而不是安靜地相減。

**已驗（2026-09-05，T001，uv 0.12.0）**：

| 問題 | 實測結果 |
|------|----------|
| 多個 `--env-file` 誰贏 | **最後一個贏** |
| 既存環境變數 vs `--env-file` | **環境變數贏**（檔案蓋不掉已 export 的變數） |

**因此定案改為疊加而非兩份完整副本**：`--env-file ../.env --env-file <覆蓋檔>`，覆蓋檔
只寫與 base 不同的那幾行。原本的「兩份各自完整」會多出一份必須同步維護的 DB／Redis／
金鑰設定，而漂掉的那天沒有徵兆。

**第二點是個陷阱，要寫進文件**：shell 裡若已 `export AI_EMBEDDING_PROVIDER`，覆蓋檔
**蓋不掉它**——評測會安靜地用錯模型。安全網仍然成立（報告記的是實際生效的值，於是兩份
報告的模型欄位會相同，`_require_comparable` 當場拒絕比較），但徵兆出現得比較晚。

---

## R-07 三檔判定放在哪一層

**Decision**：判定是**報告之外的裁決層**，實作為一個純函式（輸入兩份報告與門檻，輸出
優於／持平／劣於與理由），不寫進 `Comparison`。

**Rationale**：`Comparison.improved` 是 2B-0 定的二元判定（主指標上升且次指標不退步），
2B 系列的每一個結論都建立在它上面。把三檔塞進同一個欄位會改變既有語意；並排新增一個
函式則讓舊結論保持可重算。三檔判定純粹是數字運算，**不需要真評測就驗得動**，這是
US4「可獨立驗證」的來源。

**門檻**（來自 spec Assumptions，非本 plan 決定）：手寫題組主指標差 ≤2pp（一題的權重）
判持平；公開題組主指標退步 >0.83pp（一題的權重）觸發否決。

---

## R-08 雲端模型在 1024 維下是否自動正規化

**Decision**：以 `make verify-provider PROVIDER=gemini CAPABILITY=embedding` 實測驗證，
**不靠文件假設**。該指令會回報實際維度與向量長度（W1 對 `tei` 就是這樣驗的：回報
「1024 維、單位長度 1.000000」）。

**Rationale**：spec 的 Assumptions 明列這一條「在實測第一份報告時即可證實或推翻」。
`VENDORS["gemini"]` 的註解寫著「截斷後會自動正規化（`gemini-embedding-001` 不會）」——
那是註解，不是本機實測。若實測不是單位長度，相似度計算的前提就不成立，必須停下回報。

---

## R-09 題組擴充的形狀

**Decision**：
- 續編 `hw-25` … `hw-50`（26 題），既有 `hw-01`…`hw-24` 一字不動（FR-002b）。
- 新題的 `source` 用**新的值**（如 `ai-drafted`）與既有的 `handwritten` 區分；
  `tests/unit/test_golden_set.py` 的 `_SOURCES` 白名單同步擴充。
- 語言分布：維持既有 4 題英文，新題再補若干跨語言題（下限 FR 只要求 ≥3，實際會遠超）。
- 出題來源優先補**目前完全沒有題目的 4 份文件**。

**Rationale**：`source` 是既有欄位且語意正好是「這題哪來的」，AI 起草後人類改寫確實是
不同於純手寫的來源——用它區分比另發明一個欄位誠實。實查覆蓋率：語料涵蓋 16 份文件
（299 段），而既有 24 題只碰了 12 份，`00_專案總覽`、`01_系統架構總覽`、`02_後端專案結構`
之外的 `04_模組設計`（34 段，最大的一份）與 `13_開發Roadmap`（21 段）**一題都沒有**。
補在那裡同時提高題數與覆蓋率。

**這一步會讓一個既有測試自然變紅**：`test_the_baseline_still_matches_the_dataset_in_the_repo`
比對 baseline 報告裡的 `goldenset_sha256` 與 repo 中題組檔的實際指紋——題組一改就紅，
**要等新的 baseline 落檔才會綠**。那正是 2B-0 為這種情況設計的強制路徑，不是要修的東西。

---

## R-10 ✅ 已解決：FR-023 的「回退」方向（原為 BLOCKING）

**Status**: **RESOLVED（2026-09-05，人類裁決選項 B）。**

**當時查到的事實**：本機 `.env` 是 `AI_EMBEDDING_PROVIDER=gemini`——**W1 從來沒有把實際
使用的模型切成 `bge-m3`**（與 13 §4.2 W1 的「仍未做②：系統預設仍是 mock，本次實測是
一次性的環境變數覆寫」一致）。而 `AI_EMBEDDING_DIMENSIONS` 的程式預設已是 1024，因此
系統當時跑的是 **gemini@1024**。於是 FR-023 的「**改回**雲端模型」沒有東西可以改回，
真正的決定是它的反向。

**裁決**：**選項 B——先把系統切到地端模型，讓「回退」成立**，FR-023 字面不動。

**已執行（2026-09-05，實測驗證）**

| 步驟 | 結果 |
|------|------|
| `.env` 切至 `AI_EMBEDDING_PROVIDER=tei` / `AI_EMBEDDING_MODEL=BAAI/bge-m3` / `..._BASE_URL=http://127.0.0.1:18081/v1`（舊值註解保留，備份 `.env.bak-20260905`） | ✅ |
| `make tei-embed-up` | Healthy（模型快取有效，未重下載） |
| `make verify-provider PROVIDER=tei CAPABILITY=embedding` | ✅ 模型 `BAAI/bge-m3`、維度 **1024**、**單位長度 1.000000**、0.11s |
| `admin/民法` 重建向量（走 `KbReindexService`，`rechunk=False`） | `completed`；DB 實測 **245 筆 `BAAI/bge-m3`@1024、embedding_version 2** |
| `make tei-up`（rerank 容器當時也沒起） | Healthy |
| 實際查詢 | `hit_count=40`、`degraded=[]`、rerank `applied=True`（24 進 8 出）——W1 記載的「檢索空窗」關閉 |
| `tests/integration/test_infra_config.py` | 18 passed |

**過程中修掉的一個 W1 缺陷（本 Feature 的前置，不在其範圍內）**

第一次拿真文件打地端服務，**整批 422**：`batch size 64 > maximum allowed batch size 32`。
TEI 的 `--max-client-batch-size` 預設 32，而 `services/knowledge/embedding.py` 的
`EMBED_BATCH_SIZE` 是 **64**（06 §2.1 的設計值）。`--auto-truncate` 救不了它——那個旗標
管的是單筆過長，不是批次過大；而 422 會被退避重試打三次、每次同樣失敗，症狀是「某些
文件永遠處理不完」。修法是給容器加 `--max-client-batch-size 64`（**不動 `EMBED_BATCH_SIZE`**
——那是設計文件的值）。

**W1 的兩筆樣本實測驗不到這條路**：`verify-provider` 送的樣本數遠低於上限，於是「通過的
那個實測」與「真的能不能用」之間隔著一個沒有人看得見的門檻。

**連帶問題的處置**：「系統預設」在 repo 裡沒有對應檔案（環境設定檔不進版控、程式預設是
`mock`），因此切換模型沒有可審查的產物。spec 新增 **FR-027**：切換的日期、方向與依據必須
落進評測說明文件與開發 Roadmap。

**同時發現、未處理（不屬本 Feature 範圍，已回報）**：`BAAI/bge-m3` 不在計價表裡
（`model_price_missing`）；reindex job 的進度回報是 `total_chunks=0` 而 `embedded=245`
（2C-4 的進度條會吃到）；rerank 最高分 0.108，若啟用 06 §3.1 的 0.3 絕對門檻會被砍光；
`AI_CHAT_PROVIDER` 仍是 `mock`。

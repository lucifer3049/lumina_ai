# 評測資料（golden set 與語料）

這個目錄裝的是**檢索評測的量尺**：一組固定的問題、一份固定的語料，以及「改動之前」的
分數。它服務的是 13 §4 的 Phase 2 DoD ②——「hybrid 檢索評測優於純向量（首版 golden
set ≥100 題）」。

**這裡的東西是凍結快照，不是活資料。** 語料一旦會隨資料集版本或文件更新而變動，兩次
評測的分數就不可比——而不可比的兩個數字看起來仍然可以相減。要改，就連同 baseline 一起
重跑（見下方「什麼時候該重新取樣」）。

## 檔案

| 路徑 | 內容 | 產生方式 |
|------|------|----------|
| `corpus/drcd.jsonl` | 公開語料（繁中維基段落）1,200 段，其中 120 段是正解、其餘為干擾 | `make eval-sample SOURCE=drcd` |
| `goldenset/drcd.jsonl` | 公開題組 120 題（人寫的問句 + 標好的答案段落） | 同上 |
| `corpus/lumina_docs.jsonl` | 自家 `docs/plan/*.md` 的段落快照（約 300 段） | `make eval-sample SOURCE=docs` |
| `goldenset/handwritten.jsonl` | 手寫題 ≥20 題（含 ≥3 題英文問句 → 中文段落） | **人手寫**，不由腳本產生 |
| `reports/baseline_vector_*.json` | 改任何檢索程式**之前**的純向量分數 | `make eval-retrieval` |
| `reports/`（其餘） | 每次評測的產物 | 不進版控（見 `.gitignore`） |

格式是 JSONL（一行一筆），schema 與載入器在 `backend/rag/goldenset.py`。

## 出處與授權

公開題組與語料出自 **DRCD（Delta Reading Comprehension Dataset）**，台達電子公開的繁體
中文閱讀理解資料集：

- 專案：https://github.com/DRCKnowledgeTeam/DRCD
- 取用檔案：`DRCD_dev.json` + `DRCD_test.json`（合計約 4 MB；不用 15 MB 的 training split，
  多下載 11 MB 換不到評測價值。只用 test 則過濾後剩 1,000 段，剛好卡在 ≥1,000 的下限上）
- 授權：**CC BY-SA 3.0**（姓名標示 — 相同方式分享）
- 本 repo 收錄的是**取樣後的子集**，內容未經改寫；原始檔不進版控（見 `.gitignore`）
- 快照日期：**2026-08-23**

`corpus/lumina_docs.jsonl` 取自本 repo 自己的 `docs/plan/*.md`，同樣凍結於
**2026-08-23**；文件之後的修改不會自動反映到這裡，這是刻意的。

## 為什麼是公開題組為主

2026-08-23 拍板：**問句要出自人手**，不是用 LLM 從 chunk 生成。生成的問句會沿用段落
原文的字詞，等於天然偏袒字面檢索（FTS）——而「hybrid 是否優於純向量」正是這份題組要
回答的問題，先偏袒一邊的話，結論不管是什麼都不能信。

**已知的偏差**（誠實記錄，不是缺陷掩飾）：DRCD 的問句雖然出自人手，但寫的人看著那段
文字出題，字面重疊仍高於真實使用情境。手寫的那 20 題補的正是這一半——真實文體，以及
**跨語言**（英文問句配中文段落，06 §3.4 指名 rerank 必須多語的那個情境；pgroonga 在
跨語言天然失效，靠向量撐住）。

## 為什麼語料要有 1,200 段

題數達標不代表這把尺量得出東西。語料若只有正解那 120 段，`top_k=10` 等於一次撈走十二
分之一，純向量、hybrid、加不加 rerank 全部接近滿分——DoD ② 既證不出也推翻不了。
干擾段落不是雜訊，是這份題組的解析度。`tests/unit/test_golden_set.py` 釘著 ≥1,000 段。

## 怎麼用

```bash
# 1) 取樣（只在第一次或決定重新取樣時跑）
make eval-sample SOURCE=drcd
make eval-sample SOURCE=docs

# 2) 開通評測租戶（一次）
make demo-tenant DEMO_SLUG=lumina-eval

# 3) 跑評測（會打真的 embedding API；mock 量不出品質，會被擋下）
make eval-retrieval DATASET=drcd
make eval-retrieval DATASET=drcd EVAL_ARGS="--baseline evaluation/reports/baseline_vector_drcd.json"
```

第一次跑出來的報告要**改名為 `baseline_vector_<dataset>.json` 並提交**——那是 2B 之後
每一次比較的對照組。

## 什麼時候該重新取樣

只有兩種情況：題組被發現有系統性錯誤，或是要擴充題數。**重新取樣會讓所有既有的
baseline 失效**（報告帶著語料與題組的 sha256，`compare_reports` 靠它拒絕比較兩把不同的
尺），因此必須同時重跑 baseline，否則 `tests/unit/test_eval_runner.py` 會紅——那正是它
存在的目的。

取樣是**決定性**的（固定亂數種子 `20260823`），同樣的輸入永遠得到位元組相同的輸出。

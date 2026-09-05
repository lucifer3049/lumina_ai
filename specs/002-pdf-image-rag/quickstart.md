# Quickstart: 驗證 PDF 圖片 RAG（W2）

**Feature**: [spec.md](./spec.md) ｜ **Plan**: [plan.md](./plan.md) ｜ **Date**: 2026-09-05

本檔是**驗證指南**：照著跑，就能確認這個 Feature 真的做到了 spec 說的事。分三段，
**A 段完全不需要 GPU、模型權重或金鑰**——那是刻意的，它涵蓋了大部分的驗收面。

---

## A. 不需要 GPU、不需要任何模型的部分

這一段只用 `MockVisionProvider` 與固定的測試圖檔。**它應該全綠，而且應該在 CI 裡跑。**

### A1. 四層測試

```bash
make test-unit && make test-integration && make test-api
```

預期：全綠。與本 Feature 相關的部分可單獨跑：

```bash
make test-k K=image          # 圖片抽取、OCR 組裝、caption block
make test-k K=reextract      # 逐 KB 重新抽取的 job 與端點
make test-k K=citation       # generated / image 兩個新欄位沒有弄壞既有九欄
```

### A2. 分層邊界沒有被打破

```bash
make lint
```

預期：ruff + mypy strict + **import-linter 9/9** 全綠。

**這一項對本 Feature 特別重要**：設計把「寫物件儲存」與「呼叫 Gateway 產生描述」放在
`services/`，把 OCR 與文字組裝放在 `etl/`。如果實作時圖方便把 caption 寫進 loader，
import-linter 的 `etl 不知道上層，也不碰 ORM` 那條會紅——**那條紅不是障礙，是設計被
執行到了的證據**。

### A3. 契約沒有漂移到不該漂的地方

```bash
make openapi && make gen-api
make openapi-check
```

預期：**有漂移，且漂移只來自兩支新端點**（`documents_image_url`、
`knowledge_bases_reextract` / `knowledge_bases_reextract_status`）。

`citations` 事件多兩個欄位**不應該**造成任何 schema 變動——`MessageOut.citations` 是
`list[dict[str, Any]]` 原樣透傳。**如果它也漂了，代表有人把 citation 模型化了，那是範圍
外的改動，停下回報。**

### A4. 掃描件不再被擋在門外（unit 層，用固定樣本）

```bash
make test-file FILE=tests/unit/test_etl_loaders.py
```

預期：既有的「無文字層 → `ExtractionFailedError`」那條測試**已改寫**為「無文字層且 OCR
也產不出任何文字 → 失敗」。**這條不能直接刪**（spec US1 情境 3：不得回報成功但零段落）。

---

## B. 需要地端服務的部分

### B1. 先把 VLM 準備好

```bash
make vllm-up                    # 第三個 gpu profile 容器；模型與 tag 依 R-04 的待驗結果定
nvidia-smi                      # 兩個 TEI + vLLM 的總和必須 < 8151 MiB
```

**基準線（2026-09-05 實測）**：兩個 TEI 容器佔 5057 MiB，**餘裕只有 3.0 GB**。vLLM 的
`gpu_memory_utilization` 要夾在這個餘裕內——它是對**總量**取比例，不是對剩餘量。

**如果 VRAM 爆了**：處置是回報，**不是自行換更大的模型、也不是把 TEI 容器停掉**——那兩個
做法都會推翻 research R-04 的裁決依據（見該節的「被否決的替代方案」）。

**如果容器起不來且訊息與 CUDA 有關**：那是 R-04 的待驗項 ②（Blackwell sm_120 + WSL2）。
先看 `make vllm-logs`，比對 2B-4 為 TEI 踩出來的那套解法（`120` 標籤 ＋ tmpfs 蓋
`/usr/local/cuda/compat`）是否適用。

### B2. 實測 vision 能力

```bash
make verify-provider PROVIDER=vllm CAPABILITY=vision
```

預期輸出含：回報模型、單張耗時、描述文字前 120 字。

**這一步是自動測試驗不到的那一半**——測試看的是請求的形狀，不是「那個模型真的看得懂這張
圖」。W1 的教訓逐字適用：容器起得來不等於它載得動模型。

**結果要回寫 [research.md](./research.md) 的待驗項 ④ 與 ⑤。**

### B3. OCR 實測

```bash
make verify-ocr FILE=<一份中文掃描 PDF>   # 若 Makefile 目標尚未存在，見 plan 的 Structure Decision
```

預期：印出每頁的辨識文字前若干字、單頁耗時、以及子行程的峰值記憶體。

**峰值記憶體那一項是待驗項 ①**：沙箱的 `RLIMIT_AS` 是 1 GiB，而 ONNX Runtime 的 arena
配置可能讓位址空間遠大於常駐記憶體。**超了就調沙箱上限，不要換引擎。**

---

## C. 端到端

### C1. 掃描件走完整條路（US1）

```bash
make up
# 上傳一份純掃描 PDF（沒有文字層）
# 等它走到 ready
# 就其中一段影像上的文字提問
```

逐項對照 spec 的 Acceptance Criteria：

| 檢查 | 對應 |
|------|------|
| 文件狀態走到 `ready`（不是 `failed`） | US1 情境 1 |
| chunk 數 > 0 | US1 情境 1 |
| 問得到答案，且引用指向正確的頁 | US1 情境 2 |

### C2. 圖上的內容找得到，而且看得出是機器寫的（US2／US3）

```bash
# 上傳一份「關鍵資訊只畫在圖上、正文沒提」的 PDF
# 就那項資訊提問
```

| 檢查 | 對應 |
|------|------|
| 答得出來 | US2 情境 3 |
| 該筆引用的 `generated` 為 `true` | US3 情境 1 |
| 該筆引用的 `image` 非 `null` | US3 情境 2 的入口 |
| 展開引用後看到的是**原圖** | US3 情境 2 |
| 答案文字裡**沒有** markdown 圖片語法 | US3 情境 5 / FR-011 |

### C3. 短效連結的兩個否定條件（US3 情境 3、4）

```bash
# 取得一條連結後：
# ① 等超過 300 秒再打 → 應失敗
# ② 以另一個租戶的身分打換連結的端點 → 應 404
```

**這兩條是否定測試，比正面路徑更重要**：正面路徑壞了看得見，這兩條壞了看不見。

### C4. 成本護欄（US4）

```bash
# 上傳一份圖片數超過單份上限的文件
```

| 檢查 | 對應 |
|------|------|
| 文件仍走到 `ready` | US4 情境 1 |
| `EtlJob(stage="image").stats.skipped_over_limit` > 0 | FR-015 / SC-012 |
| `UsageLog` 中 `category="vision"` 的筆數 == 實際處理張數 | US4 情境 2 / SC-006 |
| 對同一份文件重跑（不 reingest）→ 不新增 `UsageLog` | US4 情境 4 / SC-008 |

### C5. 逐 KB 回補（US5）

```bash
curl -XPOST .../knowledge-bases/{kb_id}/reextract    # 應回 202
curl       .../knowledge-bases/{kb_id}/reextract     # 進度
```

| 檢查 | 對應 |
|------|------|
| 回補前問不到的圖片內容，回補後問得到 | US5 情境 1 |
| **回補進行中**對該 KB 提問仍得到答案 | US5 情境 2（靠既有的 `superseded` 語意） |
| 重複 POST → 409 | US5 情境 4 |
| 該 KB 有進行中的 reindex 時 POST → 409 | contracts/api.md §2.1 |

### C6. 既有行為沒有壞

```bash
make smoke
```

預期：9 passed。登入 → 上傳 → ready → 問答 → 引用這條路**完全不經過本 Feature 的任何新
程式碼**（smoke 用的不是掃描件），所以它紅了就是迴歸。

---

## D. 完成前的四項 Verification（憲章閘門 4 的前置）

```bash
make lint
make test-unit && make test-integration && make test-api
make smoke
make openapi-check
```

**四項缺一不算完成。** 其中 `openapi-check` 在本 Feature 的預期是「跑過 `make openapi &&
make gen-api` 之後無漂移」——若在**沒有**新增端點的工作包裡看到漂移，代表改到了不該改的
地方，停下回報。

---

description: "Task list for 001-eval-rebaseline"
---

# Tasks: 換 embedding 模型後的檢索品質重新定錨

**Input**: Design documents from `/specs/001-eval-rebaseline/`

**Prerequisites**: [plan.md](./plan.md)、[spec.md](./spec.md)、[research.md](./research.md)、[data-model.md](./data-model.md)、[contracts/](./contracts/)

**Tests**: MANDATORY（憲章原則 IV）。每個 story 的測試任務都排在該 story 的實作任務之前，且必須先紅、經人類 review 確認「驗對了東西」之後才開始實作。

**Test layers**：本 Feature 的驗收**集中在 `unit` 層**，這不是偷懶——它不新增端點（無 `api` 層可驗的權限矩陣）、不新增 repository、不碰租戶邊界（無 `integration` 層的 RLS 可驗）、不改變問答流程（`e2e` smoke 不受影響）。唯一的 `integration` 任務是 T049，守的是 compose 的形狀。**若實作過程中出現需要 api／integration 層的改動，代表範圍跑掉了，停下回報。**

**Work-package scope**：`/speckit-implement` 單次只跑**一個 Phase**，跑完即停等人類 review。本 Feature 對應的工作包編號**由人類在 `docs/plan/13` §4.2 指定**（W 系列，接在 W1 之後）；tasks 這邊不自行編號。

## Format: `[ID] [P?] [Story] Description`

- **[P]**：可平行（不同檔案、無未完成的相依）
- **[Story]**：US1／US2／US3／US4，對應 spec.md 的四個 user story

## ⚠ Phase 順序不照優先級，理由寫在這裡

模板要求 Phase 3+ 依 P1 → P2 → P3 排列。**本 Feature 刻意不這樣排**：US1（P1，拿到判決）的內容是 **16 次真實評測**，它在物理上不可能先做——量尺（US2 的可比性、US3 的題組、US4 的判定）不存在時，跑出來的 16 份報告既比不動也判不了。因此 Phase 依**相依順序**排，US1 落在最後。這不是把 P1 降級：**US1 仍是整包的目的，其餘三個 story 的存在理由都是讓它的結論可信。**

「MVP = 只做 US1」在這裡不成立，最小的可交付增量是 **Phase 1 + 2 + US2**（量尺本身變得可信，且完全不需要 GPU 與金鑰）。

## Path Conventions

改動全部落在 `backend/scripts/`、`backend/evaluation/`、`backend/tests/unit/` 與 repo 根的建置檔。**不動** `api/`、`services/`、`repositories/`、`ai/`、`etl/`、`tool/`、`core/`、`common/`、`apps/` 與整個 `frontend/`（plan.md 的 Structure Decision）。

---

## Phase 1: Setup（環境與切換機制）

**Purpose**：讓「兩個模型各跑一次」這件事有可靠的切換方式。**不需要 GPU 或金鑰。**

- [ ] T001 驗證 `uv run --env-file` 的覆蓋語意（research.md 的 R-06 待驗項）：多個 `--env-file` 誰贏、環境變數與檔案誰贏；驗法是跑 `--limit 1 --allow-mock` 看報告記下的 provider，結果回寫 `specs/001-eval-rebaseline/research.md` 的 R-06
- [ ] T002 [P] 在 `Makefile` 新增 `EVAL_ENV ?= ../.env`，`eval-retrieval` 目標改用 `--env-file $(EVAL_ENV)`（contracts/cli.md §1）
- [ ] T003 [P] 建立 `.env.eval-tei` 與 `.env.eval-gemini`（**不進版控**，內容須各自完整可用；`.env.eval-gemini` 不得留 `AI_EMBEDDING_BASE_URL`，否則雲端請求會打到本機容器）

**Checkpoint**：兩份 env 檔切得動，且切錯時看得出來。

---

## Phase 2: Foundational（報告的形狀——阻擋所有 story）

**Purpose**：報告記下實際維度、落在能指認模型的路徑上。US1／US2／US4 全部建立在這之上。

**⚠️ CRITICAL**：本 Phase 未完成前，任何 user story 都不能開始。

### Tests（先寫，必須先紅）⚠️

- [ ] T004 [P] unit 測試：`build_report` 產出的報告必含 `retrieval.embedding_dimensions`，於 `backend/tests/unit/test_eval_runner.py`（FR-006）
- [ ] T005 [P] unit 測試：`SCHEMA_VERSION` 為 3，且既有 version 2 報告仍讀得進來，於 `backend/tests/unit/test_eval_runner.py`（contracts/report.md §版本相容）
- [ ] T006 [P] unit 測試：預設輸出路徑為 `reports/<model_slug>/<mode>_<dataset>.json`，兩個模型跑同一模式不會互相覆蓋，於 `backend/tests/unit/test_eval_runner.py`（FR-015）

### Implementation

- [ ] T007 在 `backend/scripts/eval_retrieval.py::run_evaluation` 取**實際存下來的向量長度**（經 `EmbeddingRepository`，非 `settings.ai_embedding_dimensions`——理由見 research.md R-03），傳入 `build_report`
- [ ] T008 `backend/scripts/eval_retrieval.py::build_report` 寫入 `retrieval.embedding_dimensions`，並把 `SCHEMA_VERSION` 由 2 改為 3
- [ ] T009 `backend/scripts/eval_retrieval.py::_default_out` 改為 `REPORTS_ROOT / <model_slug> / f"{mode}_{dataset}.json"`（`+` 仍換成 `_`；model slug 需可安全當目錄名）
- [ ] T010 將既有 8 份 1536 維報告移入 `backend/evaluation/reports/legacy-gemini-1536/`（**不刪除**——它們是 2B 系列結論的證據，FR-016）
- [ ] T011 `.gitignore` 放行 `backend/evaluation/reports/` 下的模型子目錄與 `legacy-gemini-1536/`（既有規則只放行 `baseline_*.json`，因此 2B-4 那張四模式表背後的 6 份報告從來沒進過版控）

**Checkpoint**：新報告帶維度、落在帶模型的路徑上；舊報告有了明確的歷史地位。

---

## Phase 3: User Story 2 - 不會被兩個看起來可比、其實不可比的數字騙（Priority: P2）

**Goal**：量尺本身變得可信——不同模型或不同維度的兩份報告，預設拒絕相減；要比就得明說。

**Independent Test**：完全不需要 GPU、金鑰或資料庫。以人造報告餵給比較入口即可驗完。

### Tests（先寫，必須先紅）⚠️

- [ ] T012 [P] [US2] unit 測試：維度不同、模型相同 → 拒絕比較，訊息指名 `retrieval.embedding_dimensions`，於 `backend/tests/unit/test_eval_runner.py`（FR-007／US2 情境 2）
- [ ] T013 [P] [US2] unit 測試：任一邊缺 `embedding_dimensions` → 拒絕，**不得推定為維度相同**，於 `backend/tests/unit/test_eval_runner.py`（FR-008／US2 情境 3）
- [ ] T014 [P] [US2] unit 測試：模型不同且未開跨模型旗標 → 拒絕（既有行為不得回歸），於 `backend/tests/unit/test_eval_runner.py`（FR-010／US1 情境 1）
- [ ] T015 [P] [US2] unit 測試：開了跨模型旗標 → 比得出差值，且結果同時帶兩邊的 provider／model／dimensions，於 `backend/tests/unit/test_eval_runner.py`（FR-011／US1 情境 2）
- [ ] T016 [P] [US2] unit 測試：開了旗標但題組或語料指紋不同 → **仍然拒絕**（旗標只放寬模型與維度），於 `backend/tests/unit/test_eval_runner.py`（FR-012／US1 情境 3）
- [ ] T017 [P] [US2] unit 測試：rerank 模式仍強制記錄 `rerank_provider`／`rerank_model`，缺一即拒絕產出報告，於 `backend/tests/unit/test_eval_runner.py`（FR-009，防既有規則被本次改動弄鬆）
- [ ] T018 [P] [US2] unit 測試：`eval-compare` 目標存在，且未被接進 `test`／`lint`／`smoke` 與任何 CI workflow，於 `backend/tests/unit/test_eval_runner.py::TestItStaysOutOfTheAutomatedSuites`（FR-017）

### Implementation

- [ ] T019 [US2] `backend/scripts/eval_retrieval.py::_require_comparable` 納入 `retrieval.embedding_dimensions`，並新增 `allow_cross_model` 參數（放寬模型與維度，**不放寬** `dataset` 三個指紋欄位）
- [ ] T020 [US2] `backend/scripts/eval_retrieval.py::Comparison` 新增 `cross_model`、`baseline_embedding`、`candidate_embedding`（provider／model／dimensions 三元組），並讓 `_print_comparison` 印出來（data-model.md §3）
- [ ] T021 [US2] `backend/scripts/eval_retrieval.py` 新增**只比不跑**的 CLI 入口（讀兩份既有報告、不碰 DB、不打外部服務）與 `--cross-model` 旗標；可比且完成比較離場碼 0，拒絕比較為非 0（contracts/cli.md §2）
- [ ] T022 [US2] `Makefile` 新增 `eval-compare` 目標（`BASE=` / `CAND=` / `EVAL_ARGS=`）

**Checkpoint**：US2 可獨立驗收——`make test-file FILE=tests/unit/test_eval_runner.py` 全綠，且 quickstart A1 的三個指令行為正確。

---

## Phase 4: User Story 3 - 量進步的那把尺解析度足夠（Priority: P3）

**Goal**：手寫題組由 24 題擴到 ≥50 題，單題權重由 4.2% 降到 ≤2%。

**Independent Test**：不需要 GPU、金鑰或資料庫，載入題組驗結構即可。

**⚠ 本 Phase 有一步 AI 做不完**：T029 需要人類逐題改寫（FR-002）。

### Tests（先寫，必須先紅）⚠️

- [ ] T023 [P] [US3] unit 測試：手寫題組題數 ≥ 50（既有斷言下限由 20 改為 50），於 `backend/tests/unit/test_golden_set.py`（FR-001／FR-004）
- [ ] T024 [P] [US3] unit 測試：英文問句 → 中文段落的跨語言題 ≥ 3（既有斷言，確認擴充後仍成立），於 `backend/tests/unit/test_golden_set.py`
- [ ] T025 [P] [US3] unit 測試：`_SOURCES` 白名單含新增的來源值，且新舊題以 `source` 可區分，於 `backend/tests/unit/test_golden_set.py`（FR-002b）
- [ ] T026 [P] [US3] unit 測試：既有 `hw-01`…`hw-24` 的問句與正解**逐題未被改動**，於 `backend/tests/unit/test_golden_set.py`（FR-002b 後半——「結論是不是新題撐起來的」要事後查得出來）
- [ ] T027 [P] [US3] unit 測試：每題正解段落存在於語料、題目識別碼全域唯一（既有測試，確認擴充後仍綠），於 `backend/tests/unit/test_golden_set.py`（FR-003）

### Implementation

- [ ] T028 [US3] AI 起草 26 題（`hw-25`…`hw-50`），**優先補目前一題都沒有的四份文件**（`00_專案總覽`、`01_系統架構總覽`、`04_模組設計`、`13_開發Roadmap`——實查：語料涵蓋 16 份文件而既有 24 題只碰了 12 份）；起草的問句**不得沿用正解段落原文字詞作為主要檢索線索**（FR-002a）
- [ ] T029 [US3] **人類逐題改寫與確認**（FR-002）——AI 不得代勞，這是本 Feature 唯一的人類瓶頸
- [ ] T030 [US3] 將定稿的 26 題寫入 `backend/evaluation/goldenset/handwritten.jsonl`（續編 id、`source` 用新值、`language` 沿用既有白名單），**既有 24 題一字不動**
- [ ] T031 [US3] 更新 `backend/tests/unit/test_golden_set.py` 的 `_SOURCES` 與題數下限

**Checkpoint**：`make test-file FILE=tests/unit/test_golden_set.py` 全綠。

**預期同時出現一條紅**：`test_eval_runner.py::TestBaseline::test_the_baseline_still_matches_the_dataset_in_the_repo` 的 handwritten 那一條——題組指紋變了而 baseline 還沒重跑。**那條紅是設計，不是缺陷**（2B-0 的強制路徑），它要等 Phase 6 的新 baseline 落檔才轉綠。

---

## Phase 5: User Story 4 - 結論若是退步，系統要回到可用的狀態（Priority: P2）

**Goal**：把「知道」變成「做到」——三檔判定的規則存在、可重算，且只讀報告。

**Independent Test**：不需要真的退步，也不需要 GPU——以人造報告驗判定規則即可。

### Tests（先寫，必須先紅）⚠️

- [ ] T032 [P] [US4] unit 測試：判定輸出為「優於／持平／劣於」三檔之一，且可由報告重算，於 `backend/tests/unit/test_eval_runner.py`（FR-020／SC-008／US4 情境 1）
- [ ] T033 [P] [US4] unit 測試：公開題組主指標退步 > 0.83pp 時觸發否決，**即使手寫題組判為優於**，於 `backend/tests/unit/test_eval_runner.py`（FR-022／US4 情境 2）
- [ ] T034 [P] [US4] unit 測試：手寫題組主指標差 ≤ 2pp 判為持平，於 `backend/tests/unit/test_eval_runner.py`（FR-021）
- [ ] T035 [P] [US4] unit 測試：判定只讀報告——不讀環境變數、不讀設定、不打任何服務，於 `backend/tests/unit/test_eval_runner.py`（SC-008）
- [ ] T036 [P] [US4] unit 測試：`eval-verdict` 目標存在且未被接進 `test`／`lint`／`smoke`／CI，於 `backend/tests/unit/test_eval_runner.py::TestItStaysOutOfTheAutomatedSuites`

### Implementation

- [ ] T037 [US4] 在 `backend/scripts/eval_retrieval.py` 新增三檔判定的**純函式**（回傳 `level`／`decided_by`／`primary_delta`／`guard_delta`／`guard_vetoed`／`reason`），**不改 `Comparison.improved` 的既有語意**——2B 系列的每個結論都建立在它上面（research.md R-07）
- [ ] T038 [US4] 新增 CLI 入口與 `Makefile` 的 `eval-verdict` 目標（四份報告輸入，contracts/cli.md §3）；判定**不執行任何動作**

**Checkpoint**：`make test-k K=verdict` 全綠。

---

## Phase 6: User Story 1 - 拿到「換模型之後品質是好是壞」的判決（Priority: P1）🎯 本 Feature 的目的

**Goal**：16 次真實評測 → 對照表 → 判定 → 依判定行動 → 落檔。

**Independent Test**：對同一題組、同一模式的兩份報告做跨模型比較，得到帶正負號的差值與勝負判定；打開評測說明文件看得到完整對照表。

**⚠ 本 Phase 需要 GPU 與雲端金鑰**，且 16 次應在同一台機器、可行的話同一段時間內跑完（否則模型差異會混進環境差異）。

### Tests（先寫，必須先紅）⚠️

- [ ] T039 [P] [US1] unit 測試：16 份報告（2 模型 × 4 模式 × 2 題組）**皆已提交**，且每一份都能指認出 provider／model／dimensions、rerank 模式另含 reranker 資訊，於 `backend/tests/unit/test_eval_runner.py`（FR-015／FR-016／SC-005）
- [ ] T040 [P] [US1] unit 測試：`baseline_vector_*.json` 的題組與語料指紋與 repo 現況一致（既有測試，Phase 4 之後為紅、本 Phase 之後轉綠），於 `backend/tests/unit/test_eval_runner.py::TestBaseline`

### Implementation

- [ ] T041 [US1] 實測雲端模型在 1024 維下是否自動正規化：`make verify-provider PROVIDER=gemini CAPABILITY=embedding`，**不是單位長度就停下回報**（research.md R-08；spec Assumptions 唯一未證實的一條）
- [ ] T042 [US1] 跑地端模型的 8 次：`EVAL_ENV=../.env.eval-tei`，`{drcd, handwritten}` × `{vector, vector+rerank, hybrid, hybrid+rerank}`；每次確認摘要行印出的 provider／model 與該次 env 檔一致
- [ ] T043 [US1] 跑雲端模型的 8 次：`EVAL_ENV=../.env.eval-gemini`，同樣八組
- [ ] T044 [US1] 以 `make eval-compare` 加 `--cross-model` 產出 8 組對照，並以 `make eval-verdict` 得出三檔判定
- [ ] T045 [US1] **依判定行動**：優於／持平 → 不做任何設定變更；劣於（或公開題組否決）→ 依 FR-023 回退到雲端模型並重建向量至檢索可用。**回退只換 embedding 的供應商與模型，不得順帶調整任何檢索參數**（FR-025），且**不得需要 schema 變更**（FR-024，兩邊都是 1024 維）
- [ ] T046 [US1] 依最終使用的模型落定 `backend/evaluation/reports/baseline_vector_drcd.json` 與 `baseline_vector_handwritten.json`（檔名不變，語意是「系統當前實際使用的那個模型的純向量對照組」，research.md R-05）
- [ ] T047 [US1] 更新 `backend/evaluation/README.md`：2 模型 × 4 模式 × 2 題組的對照表（每格可追到報告檔）、題組擴充後的新下限與已知限制、舊報告的歷史地位（FR-018／SC-001）
- [ ] T048 [US1] 更新 `docs/plan/13_開發Roadmap.md` §4.2：W1 未做項③ 依實測結果結案、記下判定結論，並依 **FR-027** 記下切換的日期、方向與依據（實際生效的設定不進版控，文件是唯一可審查的產物）

**Checkpoint**：SC-001／SC-005／SC-007／SC-008／SC-009／SC-010 全部可驗。

---

## Phase 7: Polish & Cross-Cutting

- [ ] T049 [P] integration 測試：`docker/compose.yml` 的 `tei-embed` 帶 `--max-client-batch-size 64`，於 `backend/tests/integration/test_infra_config.py`。**守的是 spec Dependencies 記載的前置修正**（TEI 預設上限 32 小於 `EMBED_BATCH_SIZE` 64 → 任何真實文件整批 422，且退避重試每次同樣失敗）；這一條追溯到 Dependencies 而非任何 FR，是本檔唯一的例外，刻意標出
- [ ] T050 [P] `.env.example` 補上兩份評測 env 檔的用途與「`AI_EMBEDDING_BASE_URL` 會套用到任何一家 provider」的警告
- [ ] T051 執行 [quickstart.md](./quickstart.md) A 段全部指令，確認行為與預期一致
- [ ] T052 **Verification 四項**（憲章閘門 4 前置，缺一不算完成）：`make lint`、`make test-unit && make test-integration && make test-api`、`make smoke`、`make openapi-check`。**本 Feature 不動 API，`openapi-check` 應無漂移——有漂移代表改到了不該改的地方，停下回報**

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1（Setup）**：無相依，可立即開始
- **Phase 2（Foundational）**：相依 Phase 1 的 T001；**阻擋所有 user story**
- **Phase 3（US2）**：相依 Phase 2
- **Phase 4（US3）**：相依 Phase 2；**與 Phase 3 無相依**，可先做
- **Phase 5（US4）**：相依 Phase 2（判定要讀報告的形狀）；與 Phase 3／4 無相依
- **Phase 6（US1）**：相依 Phase 2 + 3 + 4 + 5 **全部**——量尺不齊時跑 16 次是白跑
- **Phase 7（Polish）**：T049／T050 隨時可做；T051／T052 必須在最後

### Within Each Story

- 測試先寫、先紅、經人類 review 之後才實作（憲章原則 IV）
- 報告形狀（Phase 2）先於任何讀寫報告的邏輯
- 純函式先於 CLI 入口，CLI 入口先於 Makefile 目標

### Parallel Opportunities

- T002／T003 可平行
- Phase 2 的三條測試（T004–T006）可平行
- Phase 3 的七條測試（T012–T018）可平行
- Phase 4 的五條測試（T023–T027）可平行
- Phase 5 的五條測試（T032–T036）可平行
- **Phase 3、4、5 三個 story 彼此無相依**，可依任意順序推進
- T042 與 T043 **不可平行**：兩者共用同一個評測知識庫與同一張 GPU

### 單人開發的現實

`[P]` 標的是「檔案不衝突、同一次可以一起做」，不是要找人平行開工。

---

## Parallel Example: User Story 2

```bash
# 一次寫完 US2 的七條測試（同一個檔案的不同測試類別，互不衝突）：
#   T012 維度不同 → 拒絕
#   T013 缺維度欄位 → 拒絕
#   T014 模型不同、未開旗標 → 拒絕
#   T015 開旗標 → 可比且帶兩邊模型資訊
#   T016 開旗標但指紋不同 → 仍拒絕
#   T017 rerank 報告仍強制記 reranker
#   T018 eval-compare 不在自動化流程內
make test-file FILE=tests/unit/test_eval_runner.py   # 預期：七條全紅
```

---

## Implementation Strategy

### 最小可交付增量（不是 US1）

**Phase 1 + Phase 2 + Phase 3（US2）**：量尺本身變得可信，且**完全不需要 GPU 與金鑰**。做完這一段，「兩份報告能不能比」這個問題就有了明確且可測的答案——而在此之前，那個答案只存在於一句「模型不同就不要比」的口頭默契裡。

### 建議的推進順序

1. Phase 1 + Phase 2 → 人類 review → 人類 commit
2. Phase 3（US2）→ Verification 四項 → 人類 review → 人類 commit
3. Phase 4（US3）→ **卡在 T029 的人類逐題改寫** → review → commit
4. Phase 5（US4）→ review → commit
5. Phase 6（US1）→ 16 次實測 → 判定 → 行動 → 文件 → review → commit
6. Phase 7 收尾

**每一步結束都要停**（憲章開發工作流：`/speckit-implement` 單次只跑一個 Phase）。

### 這個 Feature 的三個現實限制

1. **T029 卡人類**：26 題的逐題改寫，AI 不得代勞（FR-002）
2. **Phase 6 卡硬體與金鑰**：兩個 GPU 容器 + 雲端金鑰，且 16 次要在同一台機器跑完
3. **Phase 4 之後會有一條紅**：baseline 指紋守門，要等 Phase 6 才轉綠——**不要修它**

---

## Notes

- `[P]` = 不同檔案、無相依
- 測試先紅、人類 review 過再實作
- **絕不自行執行 git**（憲章 Git 安全規則）：改完輸出 Changed Files／Summary／Impact Analysis／Commit Message 建議然後停下，由人類 commit、push，再跑 `make ci-status`
- Tasks **不得**引入 `spec.md` 沒有的需求、不得改 `plan.md`、不得擴張範圍、不得做範圍外的「順手改善」（憲章原則 VI）。T049 是唯一追溯到 Dependencies 而非 FR 的任務，已在該條標明理由

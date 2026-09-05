# Implementation Plan: 換 embedding 模型後的檢索品質重新定錨

**Branch**: `001-eval-rebaseline` | **Date**: 2026-09-05 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-eval-rebaseline/spec.md`

> **原有的一項 BLOCKING 已解除**：`research.md` 的 **R-10**（FR-023 的「回退」方向與現況
> 相反）於 2026-09-05 由人類裁決選項 B——**先把系統切到地端模型，讓「回退」成立**，
> FR-023 字面不動。該切換已執行並實測驗證（含修掉一個 W1 未發現的批次上限缺陷）。
> 本計畫目前**沒有未解決的阻擋項**。

## Summary

W1 換掉 embedding 模型（雲端 → 地端、1536 → 1024 維）之後沒有任何品質數字，而既有量尺
**刻意拒絕**跨模型比較——所以這個問題目前不是「還沒量」，是「量不出來」。

本計畫的技術路線是三件事：①在報告裡補上**實際生效的向量維度**並納入可比性判斷，②開一條
**只比不跑**的顯式跨模型比較路徑（讓可比性規則不需 GPU 與金鑰就驗得動），③把手寫題組由
24 題擴到 ≥50 題。然後以 2 模型 × 4 模式 × 2 題組共 16 次真實評測產出對照表與三檔判定。

**關鍵發現（實查，非推論）**：兩個模型的向量可以並存、W1 清空向量後重跑會自動補嵌
——**這兩件事都不需要寫任何程式**。原本以為要處理的最大一塊不存在。

## Technical Context

**Language/Version**: Python 3.12（uv 管理）

**Primary Dependencies**: 既有的 `backend/rag/`（goldenset、metrics）、`backend/scripts/eval_retrieval.py`、`services/rag/retrieval.py`、`ai/gateway/`。**不引入任何新的第三方依賴。**

**Storage**: PostgreSQL 16 + pgvector（`halfvec(1024)`）——**唯讀使用，無 schema 變更**；評測資料檔為 repo 內的 JSONL 與 JSON

**Testing**: pytest。本 Feature 的驗收集中在 **unit 層**（可比性規則、三檔判定、題組守門），因為它不新增 Repository、不新增端點、不碰租戶邊界

**Target Platform**: 開發機（Linux/WSL2 + RTX 5060）；兩個 GPU 容器（TEI rerank 8080、TEI embedding 8081）

**Project Type**: 離線 CLI 工具 + 資料檔。**無 HTTP 端點、無 OpenAPI 變更、無前端**

**Performance Goals**: 不適用——這是離線評測，沒有延遲目標

**Constraints**: ① 評測不得進 `make test`／`lint`／`smoke`／CI（既有守門測試釘著）；② 語料位元組不變；③ 16 次評測應在同一台機器、可行的話同一段時間內完成，否則模型差異會混進環境差異

**Scale/Scope**: 2 模型 × 4 模式 × 2 題組 = 16 份報告；題組 24 → ≥50 題；語料 1,200 + 299 段不變

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Verdict | Notes |
|-----------|---------|-------|
| I. 單一入口與單向分層 | **PASS** | 不新增端點、不動 `api/`／`services/`。改動落在 `backend/scripts/`（維運腳本，刻意不是 Python 套件，不受 import-linter 契約管轄）、`backend/rag/goldenset.py` 的資料檔、以及 `backend/evaluation/`。9 條 contract 不受影響 |
| II. 租戶隔離 Fail Fast | **PASS** | 評測沿用既有的 `tenant_context` + `TenantScopedRepository` 路徑，不新增 repository、不新增 Redis key、不接受任何 client 輸入 |
| III. AI 呼叫收斂於 Gateway | **PASS** | embedding 與 rerank 全部經 `ai/gateway/`；本次不新增 provider、不新增 `VENDORS` 條目。切換模型只改環境變數 |
| IV. 驗收測試先行與四層測試 | **PASS** | 驗收測試先行且**大部分可獨立於外部服務**（見 quickstart A 段）。DoD 逐條回溯至 spec 的 AC——US2→可比性測試、US3→題組守門、US4→判定測試、US1→16 次實測。LLM 測試仍一律 Mock；評測本身不是測試，維持在自動化套件之外 |
| V. 契約與結構變更受控 | **PASS** | **無 migration、無 schema 變更、無 API 端點變更**，因此 `make openapi && make gen-api` 不會有 diff（`openapi-check` 應無漂移；若有即代表改到不該改的地方）。報告 JSON 的 `schema_version` 由 2 升 3，屬離線檔案格式，非 API 契約 |
| VI. 規格先行與分層授權 | **PASS** | 本計畫未改寫任何需求語意。R-10 的衝突依原則 VI 上報而非自行解決，已由人類裁決（選項 B）並回寫 spec 的 Assumptions、Dependencies 與新增的 FR-027。無殘留待決項 |

**Does this plan restate or alter any requirement in `spec.md`?** 否。R-10 的三個候選處置
是**提給人類的選項**；選定之後由 spec 自己修訂（Assumptions／Dependencies／FR-027），
不是在本計畫裡改寫。

## Project Structure

### Documentation (this feature)

```text
specs/001-eval-rebaseline/
├── plan.md              # 本檔
├── research.md          # Phase 0：10 項實查（R-10 曾為 BLOCKING，已裁決並解除）
├── data-model.md        # Phase 1：題組／報告／比較／判定四種資料的形狀
├── quickstart.md        # Phase 1：A 段（不需外部服務）＋ B 段（16 次實測）
├── contracts/
│   ├── cli.md           # Make 目標與旗標的契約
│   └── report.md        # 報告 JSON 的契約（schema 2 → 3）
├── checklists/
│   └── requirements.md  # spec 品質檢核（已通過兩輪）
└── tasks.md             # /speckit-tasks 產出——本檔不建立
```

### Source Code (repository root)

```text
backend/
├── scripts/
│   └── eval_retrieval.py        # 改：報告加維度欄位、schema 2→3、輸出路徑帶模型、
│                                #     _require_comparable 納入維度、跨模型旗標、
│                                #     只比不跑的入口、三檔判定純函式
├── rag/
│   └── goldenset.py             # 不改（`source` 的允許值本來就不寫在解析器裡）
├── evaluation/
│   ├── goldenset/handwritten.jsonl      # 改：24 → ≥50 題（新題 source 可區分）
│   ├── corpus/                          # 不改（FR-005）
│   ├── reports/<model_slug>/…           # 新增：16 份報告
│   ├── reports/legacy-gemini-1536/…     # 新增：既有 8 份移入，標為歷史
│   ├── reports/baseline_vector_*.json   # 改：依判定結果落定的純向量對照組
│   └── README.md                        # 改：新對照表、新下限、舊報告的歷史地位
└── tests/unit/
    ├── test_golden_set.py       # 改：題數下限 20→50、`_SOURCES` 擴充
    └── test_eval_runner.py      # 改：維度欄位、跨模型旗標、三檔判定、守門維持

Makefile                         # 改：EVAL_ENV 變數、eval-compare、eval-verdict 兩個目標
.gitignore                       # 改：放行 reports/ 下的模型子目錄與 legacy 目錄
.env.example                     # 改：兩份評測 env 檔的說明
docs/plan/13_開發Roadmap.md      # 改：W1 未做項③ 結案（本 Feature 結束時）
```

**Structure Decision**：改動全部落在 `backend/scripts/`、`backend/evaluation/`、
`backend/tests/unit/` 與 repo 根的建置檔。**沒有任何一筆跨越分層邊界**——`api/`、
`services/`、`repositories/`、`ai/`、`rag/`（程式部分）、`etl/`、`tool/`、`core/`、
`common/`、`apps/` 與整個 `frontend/` 都不動。9 條 import-linter contract 不受影響。

## 實作順序（依相依性，不是工時）

| # | 內容 | 相依 | 需要外部服務 |
|---|------|------|--------------|
| 1 | 驗 `uv run --env-file` 的覆蓋語意（R-06 待驗項） | — | 否 |
| 2 | 報告加 `embedding_dimensions`（schema 2→3）、可比性納入維度 | — | 否 |
| 3 | 跨模型旗標 + `eval-compare` 只比不跑的入口 | 2 | 否 |
| 4 | 三檔判定純函式 + `eval-verdict` | 2 | 否 |
| 5 | 報告輸出路徑帶模型、舊報告移入 legacy、`.gitignore` | 2 | 否 |
| 6 | 題組擴充（AI 起草 → **人類逐題改寫** → 守門測試更新） | — | 否 |
| 7 | `EVAL_ENV` 與兩份 env 檔 | 1 | 否 |
| 8 | 驗雲端模型 1024 維的正規化（R-08） | 7 | **是** |
| 9 | 16 次評測 | 2,5,6,7,8 | **是（GPU + 金鑰）** |
| 10 | 判定、對照表、文件同步、依判定行動（含 FR-027 的落檔） | 9 | 否 |

**前置已完成（2026-09-05）**：地端 embedding 實際啟用、既有知識庫向量重建、檢索實測可用，
並修掉 TEI 批次上限 32 < 64 的缺陷——**沒有那一步，第 9 步屬於地端模型的 8 次評測跑不完**。

**1–7 完全不需要 GPU 或金鑰。** 目前唯一的人類瓶頸是第 6 步的逐題改寫（FR-002）。

## Complexity Tracking

> 無憲章違反項需要辯護。R-10 不是違反，是**需求與現況的衝突**，依原則 VI 上報，已由人類
> 裁決並回寫 spec（2026-09-05）。

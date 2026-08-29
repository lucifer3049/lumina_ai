# CLAUDE.md — AI Knowledge Platform（AI 智庫平台）

本 repo 依 `docs/plan/00–15` 的架構設計文件（SAD）開發。本檔案是**每次必守的硬規則濃縮**；設計細節與理由請按下方索引查閱對應文件，不要憑記憶推測設計。

## 專案概要

Multi-tenant SaaS 的 AI Knowledge Platform：LLM Chat（SSE）、Knowledge Base、RAG（Hybrid Search + Rerank + Citation）、Tool Calling、ETL、Prompt 版本管理、Evaluation、Usage/Cost Tracking。

**技術棧**：Python 3.12 + uv／Django 5（僅 ORM + Migration + Admin）／FastAPI（唯一對外 API）／Celery + Redis／PostgreSQL 16 + pgvector(halfvec) + pgroonga／MinIO／Vue 3 + TypeScript(strict) + Pinia + pnpm／Docker Compose。

## 架構鐵則（違反即錯，無例外）

1. **FastAPI 是唯一 HTTP 入口**；Django 不對外提供業務 API。Django ORM 是 sync：一律封裝在 `repositories/` 內經 `sync_to_async` 呼叫，**禁止在 async endpoint 直接呼叫 ORM**。
2. **分層依賴單向**（import-linter 強制，9 條 contract 定義在 `backend/pyproject.toml`）：
   - `api/` → `services/` → `repositories/`、`ai/`、`rag/`、`etl/`、`tool/`；`core/` 是各層共用的基礎設施（TenantContext、業務例外、DB/Redis/物件儲存的單一入口）
   - **沒有 `core/interfaces/` 抽象層**——service 依賴的是具體 repository 類別（建構子注入 + 預設值）。單一部署單元下這是刻意取捨，不打算引入（`docs/plan/15` 的「明確不建議」清單）
   - `api/` 禁止 import `repositories/`、`apps/`（Controller 不碰 ORM）
   - `services/` 禁止 import `apps.*.models`（只能經 repository）
   - `ai/ rag/ etl/ tool/` 禁止 import `api/`、`services/`
   - `common/` 不 import 任何其他層
3. **Controller 三行原則**：解析請求 → 呼叫一個 Service 方法 → 回傳。業務邏輯、RAG、Tool 邏輯永遠不寫在 endpoint。
4. **租戶隔離**：所有 Repository 繼承 `TenantScopedRepository`（自動注入 tenant filter）；TenantContext 缺失必須 raise（Fail Fast）；Redis key 一律 `t:{tenant_id}:` 前綴；不接受 client 自報 tenant_id。
5. **LLM 呼叫只准經 AI Gateway**（`ai/gateway/`），禁止任何地方直接 import provider SDK；Prompt 一律經 PromptBuilder 使用版本化模板，禁止散落 Python string。
6. **Model 保持薄**：`apps/*/models.py` 只有欄位、Meta、`__str__`；業務規則在 Service、查詢在 Repository。
7. **Migration 只用 Django Migration**（禁 Alembic）；上線變更走三步走（加欄位帶 default → backfill → 加約束）；大表索引必用 `AddIndexConcurrently`。
8. **Celery task 三行原則**：取 context → 呼叫 service → 回報；task 必須冪等（冪等鍵 `(doc_id, doc_version, stage)` 模式）。
9. **設定與 secrets**：Pydantic Settings 讀環境變數；禁止 hardcode API key / model name / URL；secrets 不進 log、不進錯誤訊息、不進前端。
10. **前端**：`src/api/generated/` 由 OpenAPI codegen 產生，**禁止手改**；views 不直接 fetch，一律經 services/stores；LLM 輸出不用 v-html 直接渲染。

## Coding / Testing 規範

- ruff（lint+format）+ mypy strict；型別註記必寫；例外用 `core/exceptions.py` 的業務例外階層，禁止裸 `except Exception: pass`。
- 所有對外呼叫（DB/Redis/HTTP/LLM/tool）必有 timeout；retry 僅限冪等操作。
- 測試四層：unit（Service/純邏輯）、integration（Repository+RLS）、api（權限矩陣+錯誤格式）、e2e。
- **LLM 測試一律用 MockProvider，禁止呼叫真實 API**；factory_boy 對映每個 Model；tenant fixture 一律雙租戶（隔離測試內建）。
- 新增/修改資源端點時必須同步：權限 code、OpenAPI operation_id（命名穩定性視同 API 契約）、審計事件（若屬敏感操作）。

## Git 規則

### Git Safety Rule

完成任何程式修改後，禁止自行執行任何 Git 操作（包含 `git add`、`git commit`、`git push`、`git tag`、`git merge`、`git rebase` 等）。

必須先輸出：

1. Changed Files
2. Summary
3. Impact Analysis
   - Migration
   - Backward Compatibility
   - Deployment Risk
4. Commit Message 建議（格式見下）

待人類確認後，由人類自行完成 Git Commit 與 Push。

**Push 之後必跑 `make ci-status` 盯到終局**（人類或 AI 皆可跑；它會輪詢到 run 完成，紅燈時列出失敗的 job/step）。「本地全綠」不能替代這一步：CI 曾連紅四次（run 57–60）無人察覺，其中一個根因（ruff cache 假綠）恰好只在本地成立。

### Commit Message 格式

**Conventional Commits**：`type(scope): subject`

- type：`feat` / `fix` / `docs` / `refactor` / `test` / `chore` / `perf` / `ci` / `build`
- scope：受影響的層或子系統（`api`、`backend`、`infra`、`loadtest`、`test`…），跨多個以逗號分隔
- subject：≤ 72 字元、句末不加句號
- body：說明**為什麼**這樣改（改了什麼看 diff 就有）；與 subject 之間必須空一行

FastAPI / Django / Vue 生態的實務標準，且本 repo 自初始 commit 起一致採用（CONSISTENCY 原則）。Odoo 的 `[ADD]/[FIX]/[IMP]` 標籤綁 Odoo module 概念，本專案無對應物，不採用。

**產生方式**：一律走 `.gitmessage` 模板或 `git commit -F <檔案>`。**不要用單一 `-m` 貼多行**——換行會被 shell 吃掉，整段訊息塞進 subject、body 變空，`git log --oneline` 直接不可讀（`008b739`、`e262614` 已如此，因已 push 不改寫）。首次設定：`git config commit.template .gitmessage`。

## 文件索引（按任務類型讀取，不要全部載入）

| 任務類型 | 先讀 |
|----------|------|
| 任何新任務起手 | `docs/plan/00`（總覽）+ 本檔 |
| 架構決策疑問（為什麼這樣設計） | `docs/plan/01`（7 個 ADR） |
| 建立/移動檔案、找程式該放哪 | `docs/plan/02`（後端）、`docs/plan/03`（前端） |
| 實作某模組（職責/介面/依賴） | `docs/plan/04` |
| 資料表新增/變更、索引、分區 | `docs/plan/05` |
| RAG / Embedding / Memory / Gateway | `docs/plan/06` |
| Tool 新增或執行鏈修改 | `docs/plan/07` |
| ETL loader / chunker / 同步來源 | `docs/plan/08` |
| API 端點新增/修改（含 SSE 協定） | `docs/plan/09` |
| 認證/授權/加密/injection 防護 | `docs/plan/10` |
| 效能目標/擴充/降級/故障處理 | `docs/plan/11` |
| 觀測/告警/備份/CI/成本 | `docs/plan/12` |
| 排任務順序、工作包範圍、DoD | `docs/plan/13` |
| 上線驗收 | `docs/plan/14` |
| 審查結論、已知缺漏、未來功能 backlog | `docs/plan/15` |

## 開發流程（人機協作規則）

- 依 `docs/plan/13` 的 Phase 與工作包（1A、1B…）順序開發；一次任務對齊一個工作包，完成即停，等人類 review。**禁止連續自主推進多個工作包。**
- **驗收測試先行**：任務開始時先依 DoD 產出驗收測試，等人類確認測試內容後才實作，實作至測試通過為止。
- **每次任務結束必跑 E2E smoke suite**（`make smoke`：登入→上傳→ready→問答→引用）；smoke 不過視同任務未完成。
- 開發**過程中**用窄目標（`make test-changed` / `test-lf` / `test-k`，見下方常用指令）；它們是啟發式，**不是安全網**——結束前的全套 ＋ `make smoke` 那一次不能省。
- **全套目前要分三層跑**：`make test-unit && make test-integration && make test-api`（2026-08-29 實測 2039 passed，與 CI 的分階段一致）。`make test` 把三層併成單一 pytest session，已知會紅 36 條（`etl/extract/sandbox.py` 的 `run_isolated` 拿到 forkserver 的 `ConnectionRefusedError`，穩定重現，非環境殘留），那是**待單獨任務卡處理的已知缺口**（`docs/plan/13` §4 的 2C-2 結案表），不是這次改壞的；但它也不是跳過全套的藉口——分三層那一次照跑。
- 每個工作包的 DoD 在 13 內定義；測試不過、DoD 未達不得標記完成。
- 設計文件與實作衝突時：**停下並回報差異**，由人類決定改文件或改實作；不要擅自偏離文件。
- 文件值（top_k、timeout、TTL 等參數）是起始點，調整需在 PR 說明中標注並引用依據（評測/壓測數據）。

## AI 任務卡（人類下任務的格式，AI 必須遵守其邊界）

每個任務以下列格式下達；欄位缺漏時 AI 應先要求補齊再動工：

```
任務：<一句話目標>（對齊工作包：<如 1B>）
先讀：docs/plan/<編號>（§節號）
DoD 測試：<測試檔路徑或「本次先產出」>
禁區：<本次不准修改的目錄/檔案>
```

- **禁區絕對優先**：即使發現禁區內有 bug 或可優化處，只回報、不修改。
- 範圍外的「順手改善」一律禁止；發現問題記入回報清單由人類決定。
- 任務結束時回報：變更檔案清單、測試結果、smoke 結果、發現但未處理的問題。

## 常用指令

```bash
make up            # 啟動完整開發環境（Docker Compose）
# 全套（~7 分鐘；任務結束與 push 前各必跑一次）。**分三層跑**，理由見上方開發流程：
make test-unit && make test-integration && make test-api
make lint          # ruff + mypy + import-linter
make migrate       # Django migration
# 後端 schema 變更後必跑。**兩段都要**：`gen:api` 讀的是 repo 根目錄的 openapi.json，
# 單跑它只會用舊契約重產一次，看到 no diff 而以為同步了，等 CI 的 openapi-check 才紅。
make openapi && make gen-api

# 開發迴圈用的窄目標（由窄到寬，改一行時從最上面開始）：
make test-k K=credential                  # 名稱含關鍵字
make test-file FILE=tests/unit/test_x.py  # 單一檔案
make test-lf                              # 只重跑上次紅的
make test-changed                         # 依 git diff 推出受影響的測試檔
```

（實際指令以 repo Makefile 為準，尚未建立時依 `docs/plan/02` §2 的工具鏈補齊。）

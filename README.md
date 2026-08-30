# Lumina AI — AI 智庫平台

Multi-tenant SaaS 的 Enterprise AI Knowledge Platform。核心能力：LLM Chat（SSE Streaming）、Knowledge Base、RAG Pipeline（Hybrid Search + Rerank + Citation）、Tool Calling、ETL、Prompt 版本管理、Evaluation、Usage / Cost Tracking。

架構風格：**Modular Monolith + Clean Architecture + DDD**，保留未來拆分 Microservices 的能力。

> **目前狀態：Phase 1 已通過閘門（2026-08-21，有條件）；Phase 2 進行中——2A 已結案、
> 2B 做到 2B-5（KB config 寫入端驗證與 `rag_trace`），只剩 2B-6。**
>
> **狀態的單一事實來源是 [`13_開發Roadmap.md`](docs/plan/13_開發Roadmap.md)**：每個工作包的
> 範圍、DoD、結案紀錄與帶進下一包的缺口都在那裡（§2 Phase 0、§3 Phase 1 與 1A／1B／1D-5／1E
> 的結案表、§4 Phase 2 與 2A／2B-0／2B-4／2B-5 的結案表）。這一段刻意只留一行摘要——1D 時代的
> 逐包流水帳在這裡漂了三個工作包沒有人更新，而「README 說下一步是 1E」這種錯誤沒有任何
> 測試擋得住。
>
> 能力範圍（截至 2B-4）：登入與 refresh rotation、User／Tenant 管理、KB 與文件上傳、
> 五種 loader 的 ETL、embedding 與 pgvector 檢索、pgroonga 全文檢索與 RRF 融合、rerank
> （自架 TEI／Jina）、SSE 問答與引用、Prompt 版本、配額與用量、通知，以及 Vue 3 的
> 登入／KB／Chat 前端。完整設計見 [`docs/plan/`](docs/plan/)（00–15）。

---

## 技術棧

| 層級 | 選型 |
|------|------|
| 語言 / 套件管理 | Python 3.12 + [uv](https://github.com/astral-sh/uv) |
| ORM / Migration / Admin | Django 5.2 LTS（**不對外提供 HTTP API**） |
| API Layer | FastAPI + Uvicorn（唯一對外入口，REST / OpenAPI / SSE） |
| 資料庫 | PostgreSQL 16 + pgvector(halfvec) + pgroonga |
| 連線池 | PgBouncer（transaction mode） |
| 快取 / Broker | Redis |
| 背景工作 | Celery |
| 物件儲存 | MinIO |
| 前端 | Vue 3 + TypeScript(strict) + Pinia + pnpm |
| 部署 | Docker Compose |

> compose 目前只含**資料層**：PostgreSQL（pgvector + pgroonga，自建 image）/ PgBouncer /
> Redis / MinIO / Mailpit。API、Celery worker 與前端都在本機跑（`make dev` / `make start` /
> `make fe-dev`）——**應用自身的編排（compose.app.yml、healthz）尚未落地**，那是 Phase 4
> 的範圍，二次架構審計把它列為 P1（見 13 §4.1）。

## 架構鐵則

1. **FastAPI 是唯一 HTTP 入口**；Django ORM 是 sync，一律封裝在 `repositories/` 內經
   `sync_to_async` 呼叫，禁止在 async endpoint 直接呼叫 ORM。
2. **分層依賴單向**（import-linter 強制，9 條 contract 在 `backend/pyproject.toml`）：
   `api/` → `services/` → `repositories/` / `ai/` / `rag/` / `etl/` / `tool/`，`core/` 為各層共用。
   `api/` 不碰 ORM，`services/` 不 import `apps.*.models`，`common/` 不 import 任何其他層。
   **沒有 `core/interfaces/` 抽象層**：service 依賴具體 repository 類別（建構子注入），
   單一部署單元下是刻意取捨（見 [`15_計畫審查報告.md`](docs/plan/15_計畫審查報告.md)）。
3. **Controller 三行原則**：解析請求 → 呼叫一個 Service 方法 → 回傳。
4. **租戶隔離**：所有 Repository 繼承 `TenantScopedRepository`（自動注入 tenant filter）；
   TenantContext 缺失即 raise（Fail Fast）；Redis key 一律 `t:{tenant_id}:` 前綴；
   不接受 client 自報 tenant_id。
5. **LLM 呼叫只准經 AI Gateway**，Prompt 一律經 PromptBuilder 使用版本化模板。
6. **Migration 只用 Django Migration**（禁 Alembic）；大表索引用 `AddIndexConcurrently`。

完整理由見 [`01_系統架構總覽.md`](docs/plan/01_系統架構總覽.md) 的 7 份 ADR。

## 專案結構

```
backend/
  api/            FastAPI routers 與 schemas（唯一對外 HTTP 入口）
  services/       業務邏輯
  repositories/   Django ORM 存取（TenantScopedRepository）
  core/           TenantContext、業務例外階層、DB 設定
  apps/           Django models 與 migration（保持薄）
  config/         Django settings（base / dev / test）+ ASGI 掛載
  tests/          unit / integration / api / e2e（smoke）測試
  loadtest/       伺服器端延遲分析工具（負載產生腳本待 Phase 1 後重建）
  scripts/        建置與維運腳本（export_openapi.py…）
frontend/
  src/api/        client.ts（fetch 封裝）+ generated/（OpenAPI codegen 產物，禁改）
  src/types/      型別的單一 import 入口（re-export generated）
  src/router/     route 定義（lazy views）
  tests/          vitest：unit/ 與型別層 types/
openapi.json      API 契約（由 make openapi 產生，進版控）
docker/           Compose、PostgreSQL 自建 image、PgBouncer 設定樣板
docs/plan/        架構設計文件 00–15（SAD）
```

檔案該放哪：後端見 [`02_後端專案結構.md`](docs/plan/02_後端專案結構.md)，前端見
[`03_前端專案結構.md`](docs/plan/03_前端專案結構.md)。

---

## 開發環境

**前置需求**：WSL2 Ubuntu（或 Linux / macOS）、Docker、[uv](https://github.com/astral-sh/uv)、GNU Make、
Node.js 22 LTS + pnpm（ADR-007；建議用 [nvm](https://github.com/nvm-sh/nvm) 裝，pnpm 由 `corepack enable pnpm`
啟用，版本由 `frontend/package.json` 的 `packageManager` 欄位決定）。

> ⚠️ **Windows 使用者一律進 WSL2 操作，不要從 Windows 側（PowerShell / Git Bash）執行
> `make`、`uv`、`pytest`、`pnpm`。** 同一份 venv 被兩個平台交替使用時，uv 會偵測到
> 「對面平台建的 venv」而整個砍掉重建，且在 Windows 檔案鎖下常砍到一半失敗、留下不可用
> 的殘骸。Makefile 與 pytest conftest 都設有守門，非 Linux 環境會直接拒絕並說明原因。
>
> venv 位在 `~/.venvs/lumina-backend`（**repo 樹之外**，由 Makefile 的
> `UV_PROJECT_ENVIRONMENT` 指定）：實測 venv 放在 repo 裡時，即使守門健在，Windows 側
> 經 `\\wsl.localhost` 碰 repo（git、檔案總管）仍足以讓它半毀。手動跑 `uv run` 請一律
> 經 make 目標，直接在 `backend/` 下跑會在 repo 裡另長一份 `.venv`。

```bash
git clone https://github.com/lucifer3049/lumina_ai.git
cd lumina_ai
cp .env.example .env   # 填入本機用的密碼；compose 與 backend 共用這一份
make up                # 起 PG + PgBouncer + Redis + MinIO，套用 timeout、建立 bucket
make migrate           # Django migration（含 pgvector / pgroonga extension）
make verify-infra      # 基礎設施驗收測試（extension / collation / Redis / MinIO / secrets）
make gen-jwt-keys      # 產生本機 ES256 簽章金鑰（缺檔時 API 起不來）
make dev               # 另開視窗：啟動 FastAPI（http://localhost:8000）

make fe-install        # 前端相依（照 pnpm-lock.yaml）
make fe-dev            # 另開視窗：Vite（http://localhost:5173，/api 由 proxy 轉給後端）
```

`make` 不帶參數會列出所有可用指令。

**埠位**（全部走 `.env`，刻意避開預設值以免撞上本機既有服務）：PostgreSQL `15432`（僅
pytest 直連）、PgBouncer `16432`（應用端一律連這個）、Redis `16379`、MinIO `19000`
（API）/ `19001`（console）、API `8000`。

**secrets**：`.env` 已 gitignore，值不進版控；缺 `DJANGO_SECRET_KEY` / `DB_PASSWORD`
等變數時 Django 會拒絕啟動（Fail Fast），不套用開發預設值。

**三個 DB 角色**（05 §5.1、13 §3.1）：`POSTGRES_SUPERUSER` 只在 initdb 建另外兩個角色；
`DB_ADMIN_USER` 是 schema owner，跑 migration 與建 test database，直連 `15432`；
`DB_USER` 是應用執行期唯一連線，非 superuser、非 owner、無 DDL 權限，走 PgBouncer `16432`。
拆分是 PostgreSQL RLS 生效的前提——superuser 與表的 owner 都預設豁免 policy，
用同一個帳號跑全部會讓隔離在「測試全綠」的狀態下不存在。角色在 initdb 建立，
`.env` 的帳號改了要 `make clean` 重建資料卷才生效。查資料時 `make psql` 是 superuser
視角（看得到全部），`make psql-app` 才與應用看到的一致。

### 常用指令

| 指令 | 用途 |
|------|------|
| `make up` / `make down` | 啟動 / 停止基礎設施（`down` 保留資料卷） |
| `make migrate` | 執行 Django migration |
| `make test` | pytest：unit + integration + api（需先 `make up`） |
| `make test-unit` / `test-integration` / `test-api` | 分層跑測試（CI 依此分階段） |
| `make smoke` | E2E smoke suite（**每次任務結束必跑**，見下方） |
| `make verify-infra` | 只跑基礎設施驗收（`tests/integration`） |
| `make image` | 建置 backend image（與 CI 同一份 Dockerfile） |
| `make minio-init` | 重建 bucket / 版本化 / 關閉匿名存取（冪等） |
| `make db-timeouts` | 重新套用 role 層級 `statement_timeout`（冪等） |
| `make lint` | 後端 + 前端全部靜態檢查（`lint-backend` + `fe-lint`；前端需先 `make fe-install`） |
| `make lint-backend` | 只跑後端：ruff check + ruff format --check + mypy strict + import-linter |
| `make fe-install` / `fe-lint` / `fe-test` / `fe-build` / `fe-dev` | 前端相依 / eslint+vue-tsc / vitest / build / dev server |
| `make openapi` | 由 FastAPI 匯出 API 契約到 `openapi.json` |
| `make gen-api` | 由契約重新產生前端 typed client（`frontend/src/api/generated/`） |
| `make openapi-check` | 驗證契約與 generated client 未過期（CI 用） |
| `make api` / `make api-pinned` | 啟動 API（多 worker；`-pinned` 綁 CPU 供量測用） |
| `make loadtest-report` | 從 access log 算伺服器端延遲分位數 |
| `make psql` | 進 psql（直連 PG，繞過 PgBouncer） |
| `make clean` | 停止並**刪除資料卷**（清空資料庫） |

橋接旋鈕可覆寫：`make api CONN_MAX_AGE=300 ORM_THREADPOOL_SIZE=8 UVICORN_WORKERS=4`。

### 基準線量測（單機）

本專案為個人開發，沒有獨立負載產生機。負載產生器與 API 搶同一批 CPU 時，量到的是
兩者競爭的結果——實測 200 併發下 CPU 尚有 27.5% idle、DB 連線池零排隊、查詢僅
0.7ms，客戶端 p95 卻破 1 秒。因此基準線用兩項補償措施（依據與邊界見
[`docs/plan/11`](docs/plan/11_NFR_效能與可用性.md) §1.4「單機量測法」）：API 綁核心
（`make api-pinned`，log 導向 `/tmp/lumina-api.log`），以及**看伺服器端數字**：

```bash
make loadtest-report ARGS="--path /api/v1/users --last-seconds 70"
```

它讀的是 access log 的 `duration_ms`（伺服器行程內量的），不含負載產生器自己在同一
台機器上排隊的時間。兩個數字對照即可分辨「系統慢」與「壓測工具跟不上」。

> **負載產生腳本目前不存在。** 原本的 locustfile 打的是 spike 期的未認證端點
> `/api/v1/spike/*`，已隨那組端點在工作包 1A-5 一併刪除（ADR-002 結案）。重建的時機
> 是有了穩定的業務端點之後，屆時腳本要先登入拿 token 再打。

## 測試

四層：unit（Service / 純邏輯）、integration（Repository + RLS）、api（權限矩陣 + 錯誤格式）、e2e（smoke）。

- **LLM 測試一律用 MockProvider，禁止呼叫真實 API。**
- factory_boy 對映每個 Model；tenant fixture 一律雙租戶（隔離測試內建）。
- `tests/unit/` 不需任何外部依賴（含 migration 漂移、分層 contract、CI 設定、OpenAPI
  契約漂移的驗收）；`tests/integration/` 與 `tests/api/` 需先 `make up`。

```bash
make test
```

**smoke suite**（`make smoke`）不在 `make test` 裡：它會起一個真的 uvicorn 子行程、
連開發資料庫、經 `manage.py create_tenant` 建一個本輪專用租戶，前置是
`make up` + `make migrate` + `make gen-jwt-keys`。走的是**部署形狀**的伺服器，驗的是
「登入 → 上傳 → ready → 問答 → 引用」這條價值迴路仍然活著（13 §1.2：每次任務結束
必跑，不過視同任務未完成）。**五個步驟自 1D-5 起全部是實作**——1A 時只有登入是活的，其餘隨 1B–1D 逐步接上（13 §3 各結案紀錄）。

前端（03 §6.1）：vitest 跑 `frontend/tests/unit/`（API client 以 msw mock，不打真後端）
與 `frontend/tests/types/`（型別層，由 vue-tsc 檢查）。

```bash
make fe-test
```

## CI

`.github/workflows/ci.yml`（PR 與 main 的 push 觸發）分四個 job：

| Job | 內容 |
|-----|------|
| quality | `make lint-backend`（ruff + mypy strict + import-linter）、`make test-unit`。**不是 `make lint`**：那個連前端一起跑，而本 job 沒有 Node |
| tests | `make up` 起真實 PG/Redis/MinIO → `make migrate` → integration + api 測試 |
| frontend | `make fe-lint`（eslint + vue-tsc）、`make fe-test`（vitest）、`make openapi-check` |
| image | `make image` → 驗證以非 root 執行 → trivy 掃描（HIGH/CRITICAL 有修補版即擋 PR） |

兩條紀律：CI 各階段只呼叫 make target（指令不寫第二份），基礎設施只用
`docker/compose.yml`（不使用 GitHub Actions 的 `services:`）。`tests/unit/test_ci_pipeline.py`
會沿 workflow → Makefile 這條鏈驗證階段沒被拿掉。

## 程式碼規範

- ruff（lint + format，line-length 100）+ mypy strict，型別註記必寫。
- 例外用 `core/exceptions.py` 的業務例外階層，禁止裸 `except Exception: pass`。
- 所有對外呼叫（DB / Redis / HTTP / LLM / tool）必有 timeout；retry 僅限冪等操作。
- 設定與 secrets 一律走 Pydantic Settings 讀環境變數，禁止 hardcode API key / model name / URL。
- Commit 訊息採 Conventional Commits（`type(scope): subject`）。

```bash
make lint
```

## 文件索引

| 主題 | 文件 |
|------|------|
| 專案總覽與文件索引 | [00](docs/plan/00_專案總覽與文件索引.md) |
| 系統架構總覽（7 份 ADR） | [01](docs/plan/01_系統架構總覽.md) |
| 後端 / 前端專案結構 | [02](docs/plan/02_後端專案結構.md)、[03](docs/plan/03_前端專案結構.md) |
| 模組設計 | [04](docs/plan/04_模組設計.md) |
| 資料庫設計 | [05](docs/plan/05_資料庫設計.md) |
| AI Pipeline / Tool / ETL | [06](docs/plan/06_AI_Pipeline.md)、[07](docs/plan/07_Tool架構.md)、[08](docs/plan/08_ETL_Pipeline.md) |
| REST API 設計（含 SSE 協定） | [09](docs/plan/09_REST_API設計.md) |
| 安全設計 | [10](docs/plan/10_安全設計.md) |
| NFR：效能可用性 / 維運 | [11](docs/plan/11_NFR_效能與可用性.md)、[12](docs/plan/12_NFR_維運與AI特有需求.md) |
| 開發 Roadmap / 上線 Checklist | [13](docs/plan/13_開發Roadmap.md)、[14](docs/plan/14_Production_Checklist.md) |
| 計畫審查報告與 backlog | [15](docs/plan/15_計畫審查報告.md) |

---

## 授權

尚未指定授權條款。未附 LICENSE 前，本專案保留所有權利。

# Lumina AI — AI 智庫平台

Multi-tenant SaaS 的 Enterprise AI Knowledge Platform。核心能力：LLM Chat（SSE Streaming）、Knowledge Base、RAG Pipeline（Hybrid Search + Rerank + Citation）、Tool Calling、ETL、Prompt 版本管理、Evaluation、Usage / Cost Tracking。

架構風格：**Modular Monolith + Clean Architecture + DDD**，保留未來拆分 Microservices 的能力。

> **目前狀態：Phase 0 進行中。**
> 已完成：ADR-001 橋接驗證 spike（Django ORM 在 FastAPI async context 下的共存方式）、
> 開發環境基礎設施全套。尚未有業務功能，非可用產品。完整設計見 [`docs/plan/`](docs/plan/)（00–15），開發順序見
> [`13_開發Roadmap.md`](docs/plan/13_開發Roadmap.md)。

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

> compose 已含 PostgreSQL(pgvector + pgroonga，自建 image) / PgBouncer / Redis / MinIO；
> Celery、應用容器（api / worker）與前端於後續工作包接入。

## 架構鐵則

1. **FastAPI 是唯一 HTTP 入口**；Django ORM 是 sync，一律封裝在 `repositories/` 內經
   `sync_to_async` 呼叫，禁止在 async endpoint 直接呼叫 ORM。
2. **分層依賴單向**（import-linter 強制）：
   `api/` → `services/` → `core/interfaces/` ← `repositories/` / `ai/` / `rag/` / `etl/` / `tool/`。
   `api/` 不碰 ORM，`services/` 不 import `apps.*.models`，`common/` 不 import 任何其他層。
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
  tests/          unit / integration / api 測試
  loadtest/       Locust 壓測腳本
docker/           Compose、PostgreSQL 自建 image、PgBouncer 設定樣板
docs/plan/        架構設計文件 00–15（SAD）
```

檔案該放哪：後端見 [`02_後端專案結構.md`](docs/plan/02_後端專案結構.md)，前端見
[`03_前端專案結構.md`](docs/plan/03_前端專案結構.md)。

---

## 開發環境

**前置需求**：WSL2 Ubuntu（或 Linux / macOS）、Docker、[uv](https://github.com/astral-sh/uv)、GNU Make。

```bash
git clone https://github.com/lucifer3049/lumina_ai.git
cd lumina_ai
cp .env.example .env   # 填入本機用的密碼；compose 與 backend 共用這一份
make up                # 起 PG + PgBouncer + Redis + MinIO，套用 timeout、建立 bucket
make migrate           # Django migration（含 pgvector / pgroonga extension）
make verify-infra      # 基礎設施驗收測試（extension / collation / Redis / MinIO / secrets）
make seed              # 產生壓測資料（50 租戶 × 2000 筆）
make api               # 另開視窗：啟動 FastAPI（http://localhost:8000）
```

`make` 不帶參數會列出所有可用指令。

**埠位**（全部走 `.env`，刻意避開預設值以免撞上本機既有服務）：PostgreSQL `15432`（僅
pytest 直連）、PgBouncer `16432`（應用端一律連這個）、Redis `16379`、MinIO `19000`
（API）/ `19001`（console）、API `8000`、Locust web UI `8089`。

**secrets**：`.env` 已 gitignore，值不進版控；缺 `DJANGO_SECRET_KEY` / `DB_PASSWORD`
等變數時 Django 會拒絕啟動（Fail Fast），不套用開發預設值。

### 常用指令

| 指令 | 用途 |
|------|------|
| `make up` / `make down` | 啟動 / 停止基礎設施（`down` 保留資料卷） |
| `make migrate` | 執行 Django migration |
| `make test` | pytest（需先 `make up`） |
| `make test-unit` / `test-integration` / `test-api` | 分層跑測試（CI 依此分階段） |
| `make verify-infra` | 只跑基礎設施驗收（`tests/integration`） |
| `make image` | 建置 backend image（與 CI 同一份 Dockerfile） |
| `make minio-init` | 重建 bucket / 版本化 / 關閉匿名存取（冪等） |
| `make db-timeouts` | 重新套用 role 層級 `statement_timeout`（冪等） |
| `make lint` | ruff check + ruff format --check + mypy strict |
| `make loadtest` | Locust 壓測（web UI） |
| `make loadtest-headless` | 無頭跑 60 秒直接吐數字 |
| `make psql` | 進 psql（直連 PG，繞過 PgBouncer） |
| `make clean` | 停止並**刪除資料卷**（清空資料庫） |

壓測旋鈕可覆寫：`make api CONN_MAX_AGE=300 ORM_THREADPOOL_SIZE=8 UVICORN_WORKERS=4`。

## 測試

四層：unit（Service / 純邏輯）、integration（Repository + RLS）、api（權限矩陣 + 錯誤格式）、e2e。

- **LLM 測試一律用 MockProvider，禁止呼叫真實 API。**
- factory_boy 對映每個 Model；tenant fixture 一律雙租戶（隔離測試內建）。
- `tests/unit/` 不需任何外部依賴（含 migration 漂移、分層 contract、CI 設定的驗收）；
  `tests/integration/` 與 `tests/api/` 需先 `make up`。

```bash
make test
```

## CI

`.github/workflows/ci.yml`（PR 與 main 的 push 觸發）分三個 job：

| Job | 內容 |
|-----|------|
| quality | `make lint`（ruff + mypy strict + import-linter）、`make test-unit` |
| tests | `make up` 起真實 PG/Redis/MinIO → `make migrate` → integration + api 測試 |
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

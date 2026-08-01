# AI 智庫平台 —— 開發指令入口
# 執行環境：WSL2 Ubuntu（ADR-007 工具鏈：uv / pnpm）
#
# Phase 0 範圍：基礎設施全套（PG+pgvector+pgroonga、Redis、MinIO）。
# 應用容器（api/worker/beat image）、CI、smoke、前端目標於後續工作包補上。
#
# 典型流程：
#   make up → make migrate → make verify-infra
#   壓測：make seed → make api（另開視窗）→ make loadtest

# --env-file：compose 與 backend 共用 repo 根的 .env（唯一來源，見 .env.example）
COMPOSE := docker compose --env-file .env -f docker/compose.yml
BACKEND := backend
# --directory：讓指令的工作目錄落在 backend/，pytest 與 Django 才找得到
# backend/pyproject.toml 與 config/、api/、core/ 等套件。
# --env-file ../.env：路徑相對於 --directory 之後的工作目錄，故指向 repo 根的 .env。
UV      := uv --directory $(BACKEND)
UV_RUN  := $(UV) run --env-file ../.env

# 壓測旋鈕（B 組）：改這幾個值重跑，比較 rps
CONN_MAX_AGE        ?= 300
ORM_THREADPOOL_SIZE ?= 8
UVICORN_WORKERS     ?= 4

# DB 端 statement_timeout —— 值出自 11 §4.1 Timeout 全域字典（DB 5s）。
# 與 backend/config/settings/base.py 的 DB_STATEMENT_TIMEOUT 是同一個值；
# 漂移時 tests/test_db_timeouts.py 會失敗（它比對 DB 上實際生效的值）。
DB_STATEMENT_TIMEOUT ?= 5s
# 展開成一條指令而非遞迴 $(MAKE)：專案路徑含中文，WSL 下 sub-make 會 getcwd 失敗
# 並印出 `make: getcwd: No such file or directory`（指令仍會跑，但訊息會誤導人）。
# 使用者/資料庫名取自容器內的環境變數，避免與 .env 的值漂開。
APPLY_DB_TIMEOUTS = $(COMPOSE) exec -T postgres sh -c \
	'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -v ON_ERROR_STOP=1 \
	 -c "ALTER ROLE \"$$POSTGRES_USER\" SET statement_timeout = '"'"'$(DB_STATEMENT_TIMEOUT)'"'"'"'

.DEFAULT_GOAL := help
.PHONY: help up down logs psql db-timeouts minio-init migrate seed api test verify-infra lint \
        loadtest loadtest-headless clean

help: ## 顯示可用指令
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

up: ## 一鍵起環境（PG+pgvector+pgroonga / PgBouncer / Redis / MinIO）並完成初始化
	$(COMPOSE) up -d --wait
	$(APPLY_DB_TIMEOUTS)
	$(COMPOSE) run --rm minio-init

down: ## 停止基礎設施（保留資料卷）
	$(COMPOSE) down

logs: ## 追蹤基礎設施日誌
	$(COMPOSE) logs -f

psql: ## 進入 psql（直連 postgres，繞過 pgbouncer）
	$(COMPOSE) exec postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

db-timeouts: ## 套用 role 層級 statement_timeout（冪等；既有資料卷也適用）
	$(APPLY_DB_TIMEOUTS)
	@echo "statement_timeout = $(DB_STATEMENT_TIMEOUT)（下次建立連線起生效）"

minio-init: ## 建立 bucket、開啟版本化、關閉匿名存取（冪等）
	$(COMPOSE) run --rm minio-init

migrate: ## 執行 Django migration（含 pgvector / pgroonga extension）
	$(UV_RUN) python manage.py migrate

seed: ## 產生壓測資料（預設 50 租戶 × 2000 筆）
	$(UV_RUN) python manage.py seed_spike

api: ## 啟動 API（壓測目標）；可覆寫 CONN_MAX_AGE / ORM_THREADPOOL_SIZE / UVICORN_WORKERS
	CONN_MAX_AGE=$(CONN_MAX_AGE) ORM_THREADPOOL_SIZE=$(ORM_THREADPOOL_SIZE) \
	$(UV_RUN) uvicorn config.asgi:app --host 0.0.0.0 --port 8000 --workers $(UVICORN_WORKERS)

test: ## 執行全部測試（需先 make up）
	$(UV_RUN) pytest

verify-infra: ## 只跑基礎設施驗收（extension / collation / Redis / MinIO / secrets）
	$(UV_RUN) pytest tests/integration

# mypy 走 UV_RUN（帶 --env-file）：django-stubs 外掛會實際載入 config.settings.test，
# 而 settings 對 DJANGO_SECRET_KEY / DB_PASSWORD 是 Fail Fast 的。缺環境變數時 mypy 只會
# 回報「Error constructing plugin instance of NewSemanalDjangoPlugin」，指不到真正原因。
lint: ## ruff + mypy（import-linter 於 CI 工作包接入）
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV_RUN) mypy .

loadtest: ## B 組壓測：開 web UI（http://localhost:8089）
	$(UV) run --group loadtest locust -f loadtest/locustfile.py --host http://localhost:8000

loadtest-headless: ## B 組壓測：無頭跑 60 秒直接吐數字
	$(UV) run --group loadtest locust -f loadtest/locustfile.py \
		--host http://localhost:8000 --headless -u 50 -r 50 -t 60s

clean: ## 停止並刪除資料卷（會清空資料庫、Redis、MinIO）
	$(COMPOSE) down -v

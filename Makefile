# AI 智庫平台 —— 開發指令入口
# 執行環境：WSL2 Ubuntu（ADR-007 工具鏈：uv / pnpm）
#
# SPIKE 階段：本 Makefile 目前只涵蓋 ADR-001 橋接驗證所需的目標。
# Phase 0 全量時補上 smoke / gen-api / 前端相關目標。
#
# 典型流程：
#   make up → make migrate → make seed → make api（另開視窗）→ make loadtest

COMPOSE := docker compose -f docker/compose.spike.yml
BACKEND := backend
# --directory：讓指令的工作目錄落在 backend/，pytest 與 Django 才找得到
# backend/pyproject.toml 與 config/、api/、core/ 等套件。
UV      := uv --directory $(BACKEND)

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
APPLY_DB_TIMEOUTS = $(COMPOSE) exec -T postgres psql -U lumina -d lumina -v ON_ERROR_STOP=1 \
	-c "ALTER ROLE lumina SET statement_timeout = '$(DB_STATEMENT_TIMEOUT)'"

.DEFAULT_GOAL := help
.PHONY: help up down logs psql db-timeouts migrate seed api test lint loadtest loadtest-headless clean

help: ## 顯示可用指令
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

up: ## 啟動 spike 基礎設施（postgres + pgbouncer）並等待健康
	$(COMPOSE) up -d --wait
	$(APPLY_DB_TIMEOUTS)

down: ## 停止基礎設施（保留資料卷）
	$(COMPOSE) down

logs: ## 追蹤基礎設施日誌
	$(COMPOSE) logs -f

psql: ## 進入 psql（直連 postgres，繞過 pgbouncer）
	$(COMPOSE) exec postgres psql -U lumina -d lumina

db-timeouts: ## 套用 role 層級 statement_timeout（冪等；既有資料卷也適用）
	$(APPLY_DB_TIMEOUTS)
	@echo "statement_timeout = $(DB_STATEMENT_TIMEOUT)（下次建立連線起生效）"

migrate: ## 執行 Django migration
	$(UV) run python manage.py migrate

seed: ## 產生壓測資料（預設 50 租戶 × 2000 筆）
	$(UV) run python manage.py seed_spike

api: ## 啟動 API（壓測目標）；可覆寫 CONN_MAX_AGE / ORM_THREADPOOL_SIZE / UVICORN_WORKERS
	CONN_MAX_AGE=$(CONN_MAX_AGE) ORM_THREADPOOL_SIZE=$(ORM_THREADPOOL_SIZE) \
	$(UV) run uvicorn config.asgi:app --host 0.0.0.0 --port 8000 --workers $(UVICORN_WORKERS)

test: ## 執行 A 組正確性測試（需先 make up）
	$(UV) run pytest

lint: ## ruff + mypy（import-linter 於 Phase 0 全量接入）
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy .

loadtest: ## B 組壓測：開 web UI（http://localhost:8089）
	$(UV) run --group loadtest locust -f loadtest/locustfile.py --host http://localhost:8000

loadtest-headless: ## B 組壓測：無頭跑 60 秒直接吐數字
	$(UV) run --group loadtest locust -f loadtest/locustfile.py \
		--host http://localhost:8000 --headless -u 50 -r 50 -t 60s

clean: ## 停止並刪除資料卷（會清空 spike 資料庫）
	$(COMPOSE) down -v

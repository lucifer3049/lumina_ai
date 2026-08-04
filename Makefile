# AI 智庫平台 —— 開發指令入口
# 執行環境：WSL2 Ubuntu（ADR-007 工具鏈：uv / pnpm）
#
# Phase 0 範圍：基礎設施全套（PG+pgvector+pgroonga、Redis、MinIO）。
# 應用容器（api/worker/beat image）、CI、smoke、前端目標於後續工作包補上。
#
# 典型流程：
#   make up → make migrate → make verify-infra
#   壓測：make seed → make api（另開視窗）→ make loadtest

# ── 環境守門：擋掉 Windows 側執行 ────────────────────────────────────
# 同一份 backend/.venv 被 WSL2 與 Windows 交替使用時，uv 每次都會偵測到
# 「對面平台建的 venv」而整個砍掉重建；在 Windows 檔案鎖下重建常砍到一半失敗，
# 留下不可用的殘骸（2026-08-03 實際發生）。守門放這裡是因為砍 venv 發生在
# `uv run` 當下——事後才擋（例如測試裡）venv 已經沒了。
#
# 白名單而非「只准 Linux」：問題出在 Windows 的檔案鎖與路徑語意，不在 POSIX
# 平台之間。macOS（Darwin）與 README 的前置需求一致，照樣放行。
# uname 不存在時 $(shell) 回空字串 → 不在白名單 → 照樣擋下，這是要的行為。
UNAME_S := $(shell uname -s)
ifeq ($(filter Linux Darwin,$(UNAME_S)),)
$(error 本專案在 Linux / WSL2 / macOS 開發（偵測到：$(UNAME_S)）。從 Windows 側執行 uv 會毀掉 backend/.venv，請進 WSL2)
endif

# --env-file：compose 與 backend 共用 repo 根的 .env（唯一來源，見 .env.example）
COMPOSE := docker compose --env-file .env -f docker/compose.yml
BACKEND := backend
# --directory：讓指令的工作目錄落在 backend/，pytest 與 Django 才找得到
# backend/pyproject.toml 與 config/、api/、core/ 等套件。
# --env-file ../.env：路徑相對於 --directory 之後的工作目錄，故指向 repo 根的 .env。
UV      := uv --directory $(BACKEND)
UV_RUN  := $(UV) run --env-file ../.env
# CI 與本機用同一個 tag，重現問題時不必猜對方建的是哪個 image
BACKEND_IMAGE ?= lumina/backend:dev

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
.PHONY: help up down logs psql db-timeouts minio-init migrate seed api test test-unit \
        test-integration test-api verify-infra image lock-check lint loadtest \
        loadtest-headless clean

help: ## 顯示可用指令
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

up: ## 一鍵起環境（PG+pgvector+pgroonga / PgBouncer / Redis / MinIO）並完成初始化
	$(COMPOSE) up -d --wait
	# PgBouncer 只在啟動時讀設定檔，而那份檔案是每次 make up 由 pgbouncer-config
	# 重新渲染的。既有容器不會自己重載——改了 .env 的密碼之後，pgbouncer 仍以舊憑證
	# 運作，而且**完全沒有症狀**：healthcheck 也用舊密碼，照樣綠。
	# （實際踩過：改了 admin_users 之後，應用帳號仍然能下 PAUSE。）
	$(COMPOSE) up -d --wait --force-recreate pgbouncer
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

# --log-config：uvicorn 預設會給自己的 logger 掛 handler 且 propagate=False，
# 於是啟動訊息與錯誤是純文字、應用日誌是 JSON——Loki 那頭只解析得了一半。
# 這份設定把 handler 清空並改為 propagate，讓它們流進 config/logging.py 的 root handler。
# --no-access-log：存取日誌由 api/main.py 的 middleware 產生（帶 request_id/tenant_id），
# 留著 uvicorn 那份只會得到兩筆內容不同的記錄。
# ENABLE_SPIKE_ENDPOINTS=true：本 target 是**壓測目標**，locustfile 打的
# /api/v1/spike/* 與 X-Tenant-Id 取租戶都掛在這個旗標下（預設關，見 .env.example）。
# 不在這裡開，make loadtest 會整片 404——而 locust 只顯示失敗率，看不出是旗標沒開。
# 這裡用 shell 前綴而非寫進 .env：環境變數優先於 --env-file，容器部署照樣是關的。
api: ## 啟動 API（壓測目標，會開啟 spike 面）；可覆寫 CONN_MAX_AGE / ORM_THREADPOOL_SIZE / UVICORN_WORKERS
	ENABLE_SPIKE_ENDPOINTS=true \
	CONN_MAX_AGE=$(CONN_MAX_AGE) ORM_THREADPOOL_SIZE=$(ORM_THREADPOOL_SIZE) \
	$(UV_RUN) uvicorn config.asgi:app --host 0.0.0.0 --port 8000 --workers $(UVICORN_WORKERS) \
		--log-config config/uvicorn_logging.json --no-access-log

test: ## 執行全部測試（需先 make up）
	$(UV_RUN) pytest

# 分層目標對應 02 §2 的測試四層；CI 分階段跑（unit 最快，壞掉時最好定位）。
test-unit: ## 只跑 unit（無外部依賴，不需 make up）
	$(UV_RUN) pytest tests/unit

test-integration: ## 只跑 integration（Repository / 基礎設施；需先 make up）
	$(UV_RUN) pytest tests/integration

test-api: ## 只跑 api（權限矩陣、錯誤格式、SSE 協定；需先 make up）
	$(UV_RUN) pytest tests/api

# 只挑 test_infra_*.py（-k 會比對 module 名，不用 shell glob——glob 會在 repo 根展開，
# 而 pytest 的工作目錄是 backend/，路徑對不上）。與 test-integration 的差別在此：
# 那個目標跑整個 integration 層（含 bridge / tenant scope / db timeout）。
verify-infra: ## 只跑基礎設施驗收（extension / collation / Redis / MinIO / secrets）
	$(UV_RUN) pytest tests/integration -k infra

image: ## 建置 backend image（與 CI 同一份 Dockerfile）
	docker build -t $(BACKEND_IMAGE) $(BACKEND)

lock-check: ## 驗證 uv.lock 與 pyproject 一致（唯讀，不會改動 lock）
	$(UV) lock --check

# mypy 走 UV_RUN（帶 --env-file）：django-stubs 外掛會實際載入 config.settings.test，
# 而 settings 對 DJANGO_SECRET_KEY / DB_PASSWORD 是 Fail Fast 的。缺環境變數時 mypy 只會
# 回報「Error constructing plugin instance of NewSemanalDjangoPlugin」，指不到真正原因。
# lock-check 是 lint 的前置：`uv run` 在鎖檔過期時會**自動重新解析並就地更新
# uv.lock**，於是「改了 pyproject 卻忘了提交新 lock」的 PR 會全綠通過，
# 之後才在 Dockerfile 的 `uv sync --frozen` 爆掉（或 image 與 CI 裝到不同版本）。
# CI 另外獨立跑一次 lock-check，**這個重複是刻意的**：CI 那步是為了讓紅燈一眼看出
# 是鎖檔問題，這個前置則是讓本機 `make lint` 同樣受保護。拿掉任一邊都會少一半覆蓋。
lint: lock-check ## uv.lock 檢查 + ruff + mypy + import-linter（分層依賴強制）
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV_RUN) mypy .
	$(UV) run lint-imports

# PYTHONUTF8=1：locust 啟動時會自動探測設定檔，其中包含 backend/pyproject.toml
# （找 [tool.locust] 區段），並以**該環境的預設編碼**開檔。該檔有中文與破折號，
# 預設編碼不是 UTF-8 時 locust 會在壓測開始前就死在
# 「Couldn't parse TOML file: 'cp950' codec can't decode byte 0xe2」——web UI 連開都開不了。
#
# 誠實標註現況：實際咬到人的是 Windows 繁中的 cp950（2026-08-03），而上方的平台
# 守門已經把 Windows 擋在門外，所以這行**目前是防禦性的，不是活的修復**。
# 保留而非刪除的理由：locust 也可能被繞過 make 直接以 `uv run locust` 啟動。
# 實測過 Linux 端不需要它——Ubuntu 24.04 + Python 3.12 即使 LC_ALL=C，
# 因 PEP 538/540 的 C locale coercion，預設編碼仍是 utf-8（"LANG=C 會炸" 是誤解）。
LOCUST := PYTHONUTF8=1 $(UV) run --group loadtest locust -f loadtest/locustfile.py

loadtest: ## B 組壓測：開 web UI（http://localhost:8089）
	$(LOCUST) --host http://localhost:8000

loadtest-headless: ## B 組壓測：無頭跑 60 秒直接吐數字
	$(LOCUST) --host http://localhost:8000 --headless -u 50 -r 50 -t 60s

clean: ## 停止並刪除資料卷（會清空資料庫、Redis、MinIO）
	$(COMPOSE) down -v

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

# ── 單機量測的補償措施（11 §1.4 已知偏離）────────────────────────────
# 本專案為個人開發，無獨立負載產生機。locust 與 API 搶同一批核心時量到的是
# 兩者競爭的結果——2026-08-05 實測 200 併發下 CPU 尚有 27.5% idle、DB 連線池
# 零排隊（cl_waiting=0、sv_idle=20）、DB 查詢僅 0.715ms，p95 卻破 1 秒。
# 綁核心讓兩邊各有專屬 CPU，前後比較才有意義。
#
# **這不等於分機量測**：記憶體頻寬、L3、DB 容器仍共用，仍不足以取代 §1.4 的
# 分機要求。用途是「同一台機器上的可重現基準線」，不是絕對值認證。
# 核心編號依 nproc 調整；預設假設 6 核（API 4 顆、負載 2 顆）。
API_CPUS   ?= 0-3
LOAD_CPUS  ?= 4-5
# 壓測參數集中在此，基準線才可重現（DoD 值見 13 Phase 0）
LOAD_USERS ?= 200
LOAD_RATE  ?= 50
LOAD_TIME  ?= 60s
# 伺服器端延遲分析的輸入：api-pinned 的 stdout 導向此檔
API_LOG    ?= /tmp/lumina-api.log

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
.PHONY: help up down logs psql db-timeouts minio-init migrate seed dev api api-pinned \
        test test-unit test-integration test-api verify-infra image lock-check lint \
        loadtest loadtest-headless loadtest-pinned loadtest-report clean

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
#
# 指令拆成 API_ENV / API_ARGS 兩個變數，是為了讓 api 與 api-pinned 共用同一份
# 定義——兩份會漂，而漂掉時症狀是「綁核心那組跑出不同數字」，看起來像 CPU
# 綁定的效果，其實只是參數不同。
API_ENV  = ENABLE_SPIKE_ENDPOINTS=true CONN_MAX_AGE=$(CONN_MAX_AGE) ORM_THREADPOOL_SIZE=$(ORM_THREADPOOL_SIZE)
API_ARGS = config.asgi:app --host 0.0.0.0 --port 8000 --workers $(UVICORN_WORKERS) \
	--log-config config/uvicorn_logging.json --no-access-log

api: ## 啟動 API（壓測目標，會開啟 spike 面）；可覆寫 CONN_MAX_AGE / ORM_THREADPOOL_SIZE / UVICORN_WORKERS
	$(API_ENV) $(UV_RUN) uvicorn $(API_ARGS)

# taskset 放在 uv 之前：affinity 由子行程繼承，uvicorn 的 worker 全都會落在同一組核心。
api-pinned: ## 啟動 API 並綁定 CPU $(API_CPUS)（基準線用）。log 導向 $(API_LOG) 供 loadtest-report 分析
	$(API_ENV) taskset -c $(API_CPUS) $(UV_RUN) uvicorn $(API_ARGS) 2>&1 | tee $(API_LOG)

# ── 日常開發伺服器 ──────────────────────────────────────────────
# 與 api（壓測目標）刻意分開，三個場景三組值：
#   dev  = 1 worker + --reload：斷點會停在你設的那行、改完自動重啟、log 是人看的
#   api  = $(UVICORN_WORKERS) workers：要壓榨核心量吞吐，斷點與熱重載都不適用
#   部署 = 每 replica 2（01 附錄 A 註 1）
# uvicorn 的 --reload 與 --workers > 1 互斥，本來就不能兩者兼得；多行程下中斷點
# 落在 fork 出去的子行程，IDE 接不到——這是「開發用單 worker」的真正理由，
# 與效能無關（同 Odoo 本地 workers=0 走 threaded 模式的道理）。
#
# 不帶 ENABLE_SPIKE_ENDPOINTS：spike 面無認證且違反 ADR-002，開發環境沒有理由開。
# 因此 Phase 0 現階段起來後只有 /docs 與 /openapi.json，沒有業務端點——這是預期
# 的（業務端點自 Phase 1 起才出現），不是壞掉。要打 spike 端點請用 make api。
#
# --host 127.0.0.1（而非 api 的 0.0.0.0）：開發機不需要對區網開放。
#
# 熱重載在本專案的環境下需要兩項設定，**缺任一項都會靜默失效**（沒有錯誤訊息，
# 只是改檔後永遠不重啟）。兩項都是實測出來的：
#
# 1. WATCHFILES_FORCE_POLLING=1
#    uvicorn 的 --reload 交給 watchfiles，後者預設走 inotify；本 repo 位於
#    /mnt/d（WSL2 的 DrvFs，Windows 檔案系統的轉接層）而 **DrvFs 不支援 inotify**
#    ——事件永遠不會來。改成輪詢即可偵測。
#
# 2. --reload-dir 限定在原始碼目錄
#    uvicorn 預設監看整個工作目錄，而 backend/ 底下 13,028 個檔案有 12,836 個
#    在 .venv/ 裡（98.5%）。輪詢模式要逐一 stat，在 9p 上慢到形同沒有監看
#    （實測：監看 . 時改檔 30 秒無反應；限定 api/ 後立即偵測到）。
#
# 新增頂層套件（common/、ai/、rag/…）時必須同步加進來，否則那個目錄的改動不會
# 觸發重載——由 tests/unit/test_logging.py 的 DEV_RELOAD_DIRS 對帳測試擋住。
# 若日後把 repo 搬進 WSL2 原生檔案系統（~/），兩項都可以拿掉。
DEV_RELOAD_DIRS = api apps config core repositories services

dev: ## 開發伺服器：單 worker + 熱重載 + console log（不開 spike 面）
	LOG_FORMAT=console WATCHFILES_FORCE_POLLING=1 \
	$(UV_RUN) uvicorn config.asgi:app --host 127.0.0.1 --port 8000 \
		--reload $(addprefix --reload-dir ,$(DEV_RELOAD_DIRS)) \
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
LOCUST_BIN := $(UV) run --group loadtest locust -f loadtest/locustfile.py
LOCUST     := PYTHONUTF8=1 $(LOCUST_BIN)
# 環境變數前綴在 taskset 之前：shell 先設環境，taskset 再 exec locust，env 照樣繼承。
LOCUST_PIN  = PYTHONUTF8=1 taskset -c $(LOAD_CPUS) $(LOCUST_BIN)

loadtest: ## B 組壓測：開 web UI（http://localhost:8089）
	$(LOCUST) --host http://localhost:8000

loadtest-headless: ## B 組壓測：無頭跑 60 秒直接吐數字
	$(LOCUST) --host http://localhost:8000 --headless -u 50 -r 50 -t 60s

# 基準線三步：make api-pinned（另一視窗）→ make loadtest-pinned → make loadtest-report
loadtest-pinned: ## 基準線壓測：負載綁 CPU $(LOAD_CPUS)，參數走 LOAD_USERS/LOAD_RATE/LOAD_TIME
	$(LOCUST_PIN) --host http://localhost:8000 --headless \
		-u $(LOAD_USERS) -r $(LOAD_RATE) -t $(LOAD_TIME)

# locust 的 p95 是**客戶端**量的，含它自己在同一台機器上排隊的時間；access log 的
# duration_ms 是**伺服器內部**量的。兩者對照即可分辨「系統慢」與「壓測工具跟不上」。
# --last-seconds 比 LOAD_TIME 多 10 秒：涵蓋整輪又不吃進上一輪（log 是持續附加的，
# API 不重啟連跑多輪時不篩時間會拿到混合值）。
loadtest-report: ## 從 $(API_LOG) 算伺服器端延遲分位數（只看最後一輪）
	$(UV) run python loadtest/analyze_access_log.py $(API_LOG) \
		--path /api/v1/spike/items --last-seconds $$(( $(LOAD_TIME:s=) + 10 ))

clean: ## 停止並刪除資料卷（會清空資料庫、Redis、MinIO）
	$(COMPOSE) down -v

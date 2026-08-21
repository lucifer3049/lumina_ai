# AI 智庫平台 —— 開發指令入口
# 執行環境：WSL2 Ubuntu（ADR-007 工具鏈：uv / pnpm）
#
# 典型流程：
#   make up → make migrate → make gen-jwt-keys → make test → make smoke
#   前端：make fe-install → make fe-test（後端 schema 改過就先 make openapi gen-api）
#
# **壓測鏈於 1A-5 隨 spike 面一起移除**：locustfile 打的是 /api/v1/spike/items、
# 資料由 seed_spike 產生，兩者都是 ADR-002 已知偏離的一部分（未認證、client 自報
# 租戶）。留著會得到一個必然 404 的壓測——比沒有壓測更糟，因為它跑得起來。
# 重建時機是有了真的業務端點之後（Phase 1 之後），屆時 locustfile 要先登入拿 token。
# 伺服器端延遲分析（loadtest/analyze_access_log.py）不受影響，它讀的是 access log。

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
FRONTEND := frontend
# **必須 cd 進去，不能用 `pnpm --dir frontend`**（2026-08-09 修）。
#
# `--dir` 只改變 pnpm 自己的工作目錄，而版本是**在 pnpm 啟動之前**由 corepack 決定的：
# corepack 從 **cwd** 往上找帶 `packageManager` 欄位的 package.json，repo 根沒有這個
# 檔案（前端的它看不到），於是它改抓 registry 上的 latest。抓到的版本與
# frontend/package.json 釘的版本一旦不同，pnpm 一啟動就拒跑：
#
#   [ERROR] This project is configured to use 11.20.0 of pnpm. Your current pnpm is v11.21.0
#
# 這個錯誤在 pnpm 發下一版之前**完全不會出現**——latest 恰好等於我們釘的版本時，
# corepack 猜對了。也就是說 packageManager 這個釘子從第一天起就沒有生效，只是碰巧
# 沒露餡（2026-08-09 pnpm 發 11.21.0 當天 CI 全紅，而前一次 CI 全綠）。
# 本機不一定重現得出來：本機的 corepack 有備妥的版本，不會每次去抓 latest。
#
# 不在 repo 根補一個只有 packageManager 的 package.json：那讓版本號變成兩份，
# 而兩份漂掉時的症狀就是上面這個錯誤。
PNPM    := cd $(FRONTEND) && pnpm
# 前端 codegen 的輸入與輸出（09 §4、03 §3.1）。兩者都進版控，漂移由 openapi-check 擋。
CONTRACT  := openapi.json
GENERATED := $(FRONTEND)/src/api/generated
# CI 與本機用同一個 tag，重現問題時不必猜對方建的是哪個 image
BACKEND_IMAGE ?= lumina/backend:dev

# 橋接旋鈕（ADR-001）：改這幾個值重跑，比較 rps
CONN_MAX_AGE        ?= 300
ORM_THREADPOOL_SIZE ?= 8
UVICORN_WORKERS     ?= 4

# ── 單機量測的補償措施（11 §1.4 已知偏離）────────────────────────────
# 本專案為個人開發，無獨立負載產生機。負載產生器與 API 搶同一批核心時量到的是
# 兩者競爭的結果——2026-08-05 實測 200 併發下 CPU 尚有 27.5% idle、DB 連線池
# 零排隊（cl_waiting=0、sv_idle=20）、DB 查詢僅 0.715ms，p95 卻破 1 秒。
# 綁核心讓兩邊各有專屬 CPU，前後比較才有意義。
#
# **這不等於分機量測**：記憶體頻寬、L3、DB 容器仍共用，仍不足以取代 §1.4 的
# 分機要求。用途是「同一台機器上的可重現基準線」，不是絕對值認證。
# 核心編號依 nproc 調整；預設假設 6 核（API 4 顆、負載 2 顆）。
API_CPUS   ?= 0-3
# 伺服器端延遲分析的輸入：api-pinned 的 stdout 導向此檔
API_LOG    ?= /tmp/lumina-api.log

# DB 端 statement_timeout —— 值出自 11 §4.1 Timeout 全域字典（DB 5s）。
# 與 backend/config/settings/base.py 的 DB_STATEMENT_TIMEOUT 是同一個值；
# 漂移時 tests/test_db_timeouts.py 會失敗（它比對 DB 上實際生效的值）。
DB_STATEMENT_TIMEOUT ?= 5s
# 展開成一條指令而非遞迴 $(MAKE)：專案路徑含中文，WSL 下 sub-make 會 getcwd 失敗
# 並印出 `make: getcwd: No such file or directory`（指令仍會跑，但訊息會誤導人）。
# 使用者/資料庫名取自容器內的環境變數，避免與 .env 的值漂開。
#
# **只套應用角色**（13 §3.1 的 1A-P3）：migration 角色若也吃這個 5s 上限，
# 大表的 AddIndexConcurrently 與 HNSW 建索引會在中途被砍
# （`canceling statement due to statement timeout`），而那時 schema 已是半套的。
APPLY_DB_TIMEOUTS = $(COMPOSE) exec -T postgres sh -c \
	'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -v ON_ERROR_STOP=1 \
	 -c "ALTER ROLE \"$$DB_USER\" SET statement_timeout = '"'"'$(DB_STATEMENT_TIMEOUT)'"'"'"'

# ── 一鍵啟停（make start / stop / status）────────────────────────
# pid/log 的實際路徑由 scripts/dev.sh 決定（repo 根的 .run/，已 gitignore）；
# process group 的處理（setsid）也在那裡，recipe 本身不需要 bash 或 set -m。
DEV_PORT ?= 8000
FE_PORT  ?= 5173

# 開發帳號（只給本機實測用；正式開通走 make demo-tenant 之外的正常流程）
DEMO_SLUG     ?= demo
DEMO_EMAIL    ?= owner@demo.local
DEMO_PASSWORD ?= demo-password-1234

.DEFAULT_GOAL := help
# start 的前置鏈（容器 → migration → 金鑰）依賴序列執行；-j 下 make 會把同一目標
# 的前置們平行跑，migrate 會在 postgres 就緒前連線、API 會在金鑰落地前啟動。
# 本檔是指令選單，平行化沒有收益，整份關掉最直接。
.NOTPARALLEL:
.PHONY: help up down logs psql psql-app db-timeouts minio-init gen-jwt-keys migrate \
        dev api api-pinned start stop restart status demo-tenant app-logs \
        test test-unit test-integration test-api smoke verify-infra verify-provider \
        image lock-check lint lint-backend ci-status \
        fe-install fe-lint fe-test fe-build fe-dev openapi gen-api openapi-check \
        loadtest-report clean

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

# 兩個 psql 入口，因為「用哪個角色連進去」會改變你看到的東西：superuser 豁免
# RLS 與所有權限檢查，用它查資料會看到 policy 之外的全貌——排查隔離問題時
# 那正是最容易誤導人的視角。
psql: ## 進入 psql（superuser，break-glass 用；豁免 RLS，查隔離問題請用 psql-app）
	$(COMPOSE) exec postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

psql-app: ## 進入 psql（應用角色，看到的與應用一致：受 RLS 與權限限制）
	$(COMPOSE) exec postgres sh -c \
		'PGPASSWORD="$$DB_PASSWORD" psql -h 127.0.0.1 -U "$$DB_USER" -d "$$POSTGRES_DB"'

db-timeouts: ## 套用 role 層級 statement_timeout（冪等；既有資料卷也適用）
	$(APPLY_DB_TIMEOUTS)
	@echo "statement_timeout = $(DB_STATEMENT_TIMEOUT)（下次建立連線起生效）"

minio-init: ## 建立 bucket、開啟版本化、關閉匿名存取（冪等）
	$(COMPOSE) run --rm minio-init

# JWT 簽章金鑰（10 §2.1 的 ES256）。私鑰不進版控——backend/.secrets/ 已 gitignore。
# 為什麼不讓程式在啟動時自動產生：那會讓「忘了掛金鑰」的部署照樣起得來，而每次
# 重啟金鑰就換一組，症狀是使用者隨機被登出，根因極難查。缺檔時 Fail Fast 比較好。
JWT_KEY_DIR ?= $(BACKEND)/.secrets

gen-jwt-keys: ## 產生本機用的 ES256 金鑰對（已存在則不覆蓋）
	@mkdir -p $(JWT_KEY_DIR)
	@if [ -f $(JWT_KEY_DIR)/jwt-es256.key ]; then \
		echo "已存在 $(JWT_KEY_DIR)/jwt-es256.key，未覆蓋（要重產請先手動刪除）"; \
	else \
		openssl ecparam -name prime256v1 -genkey -noout \
			| openssl pkcs8 -topk8 -nocrypt -out $(JWT_KEY_DIR)/jwt-es256.key; \
		openssl ec -in $(JWT_KEY_DIR)/jwt-es256.key -pubout -out $(JWT_KEY_DIR)/jwt-es256.pub; \
		chmod 600 $(JWT_KEY_DIR)/jwt-es256.key; \
		echo "已產生 $(JWT_KEY_DIR)/jwt-es256.{key,pub}"; \
	fi

# --database=admin：migration 必須以 schema owner 執行（13 §3.1）。
# 走 default（應用角色）會直接因缺 DDL 權限而失敗——那是好的；危險的是有人為了
# 讓它過而把 CREATE 權限補回應用角色，那會讓每張新表的 owner 變成應用角色，
# 而 owner 預設豁免 RLS policy。
migrate: ## 執行 Django migration（以 owner 角色；含 pgvector / pgroonga extension）
	$(UV_RUN) python manage.py migrate --database=admin

# --log-config：uvicorn 預設會給自己的 logger 掛 handler 且 propagate=False，
# 於是啟動訊息與錯誤是純文字、應用日誌是 JSON——Loki 那頭只解析得了一半。
# 這份設定把 handler 清空並改為 propagate，讓它們流進 config/logging.py 的 root handler。
# --no-access-log：存取日誌由 api/main.py 的 middleware 產生（帶 request_id/tenant_id），
# 留著 uvicorn 那份只會得到兩筆內容不同的記錄。
#
# 指令拆成 API_ENV / API_ARGS 兩個變數，是為了讓 api 與 api-pinned 共用同一份
# 定義——兩份會漂，而漂掉時症狀是「綁核心那組跑出不同數字」，看起來像 CPU
# 綁定的效果，其實只是參數不同。
# ETL worker 的並行度。預設 2：抽取本身又會 fork 一個子行程（08 §6 的隔離），
# 本機開太多只是讓風扇轉得比較大聲。正式環境依 11_NFR 的佇列深度調整。
ETL_CONCURRENCY ?= 2

API_ENV  = CONN_MAX_AGE=$(CONN_MAX_AGE) ORM_THREADPOOL_SIZE=$(ORM_THREADPOOL_SIZE)
API_ARGS = config.asgi:app --host 0.0.0.0 --port 8000 --workers $(UVICORN_WORKERS) \
	--log-config config/uvicorn_logging.json --no-access-log

api: ## 啟動 API（多 worker，量測用）；可覆寫 CONN_MAX_AGE / ORM_THREADPOOL_SIZE / UVICORN_WORKERS
	$(API_ENV) $(UV_RUN) uvicorn $(API_ARGS)

# taskset 放在 uv 之前：affinity 由子行程繼承，uvicorn 的 worker 全都會落在同一組核心。
api-pinned: ## 啟動 API 並綁定 CPU $(API_CPUS)（基準線用）。log 導向 $(API_LOG) 供 loadtest-report 分析
	$(API_ENV) taskset -c $(API_CPUS) $(UV_RUN) uvicorn $(API_ARGS) 2>&1 | tee $(API_LOG)

# ── 日常開發伺服器 ──────────────────────────────────────────────
# 與 api（量測用）刻意分開，三個場景三組值：
#   dev  = 1 worker + --reload：斷點會停在你設的那行、改完自動重啟、log 是人看的
#   api  = $(UVICORN_WORKERS) workers：要壓榨核心量吞吐，斷點與熱重載都不適用
#   部署 = 每 replica 2（01 附錄 A 註 1）
# uvicorn 的 --reload 與 --workers > 1 互斥，本來就不能兩者兼得；多行程下中斷點
# 落在 fork 出去的子行程，IDE 接不到——這是「開發用單 worker」的真正理由，
# 與效能無關（同 Odoo 本地 workers=0 走 threaded 模式的道理）。
#
# --host 127.0.0.1（而非 api 的 0.0.0.0）：開發機不需要對區網開放。
#
# 熱重載需要 --reload-dir 限定在原始碼目錄：uvicorn 預設監看整個工作目錄，而
# backend/ 底下 13,028 個檔案有 12,836 個在 .venv/ 裡（98.5%）。inotify 是每個檔案
# 一個 watch，而 watch 數有上限（fs.inotify.max_user_watches）——監看 .venv 等於把
# 額度耗在永遠不會改的檔案上。
#
# 新增頂層套件（common/、ai/、rag/…）時必須同步加進來，否則那個目錄的改動不會
# 觸發重載——由 tests/unit/test_logging.py 的 DEV_RELOAD_DIRS 對帳測試擋住。
#
# **WATCHFILES_FORCE_POLLING 已移除**（2026-08-15，repo 從 /mnt/d 搬進 ~/ 之後）。
# 它當初是必要的：DrvFs 不支援 inotify，事件永遠不會來，只能退回輪詢。代價是
# 輪詢在沒有任何改動時也持續 stat 全部來源檔——那是 dev server 閒置時的固定 CPU
# 佔用。ext4 上 inotify 正常運作，事件驅動、閒置時零成本。
# 若哪天又在 /mnt/* 底下開發，這一項要加回來，否則熱重載會**靜默失效**
# （沒有錯誤訊息，只是改檔後永遠不重啟）。
DEV_RELOAD_DIRS = ai api apps common config core etl rag repositories services worker

DEV_CMD = LOG_FORMAT=console \
	$(UV_RUN) uvicorn config.asgi:app --host 127.0.0.1 --port $(DEV_PORT) \
		--reload $(addprefix --reload-dir ,$(DEV_RELOAD_DIRS)) \
		--log-config config/uvicorn_logging.json --no-access-log

dev: ## 開發伺服器：單 worker + 熱重載 + console log（前景，Ctrl-C 停止）
	$(DEV_CMD)

# 背景 worker（08 §1：專屬佇列與行程，不與 default 爭資源）。
# 前景執行，Ctrl-C 停止；沒有它的話上傳的文件會停在 uploaded——訊息進了佇列但
# 沒有人處理，而那在 API 側完全看不出來。
#
# **兩條佇列都要吃**：etl 是解析與切塊（吃 CPU），embedding 是算向量（吃外部 API 的
# 等待時間）。少吃一條的症狀與「worker 沒起來」一模一樣，只是卡住的位置不同——漏了
# embedding 的話文件停在 chunked，而 API 一樣回 201、佇列深度一樣正常。
# 單一行程吃兩條是開發環境的選擇；正式環境依 08 §1 分開部署，兩者的擴縮依據不同。
# `python -m celery` 而不是 `celery`：主控台腳本的 sys.path[0] 是 bin 目錄，工作目錄
# 不在路徑上，於是 worker 啟動時 `autodiscover_tasks(["worker"])` 會 ModuleNotFoundError
# （而 `-A config.celery_app` 反而先過了，錯誤看起來像是 worker 套件不存在）。
# `-m` 會把工作目錄放進 sys.path，與 smoke 的 worker fixture 走同一條路。
#
# **`--pool threads` 是必要條件，不是偏好**（1C-4 實測）。Celery 預設的 prefork pool
# 把工作行程建成 daemonic，而 **daemonic 行程不准有子行程**——抽取正是跑在子行程裡
# （08 §6 的隔離）。用預設值時每一次上傳都會撞：
#
#   AssertionError: daemonic processes are not allowed to have children
#
# 症狀是文件永遠停在 `parsing`，重試 30s/2m/10m 之後才失敗，而 API 側一切正常。
# 這個缺陷從 1B-6 就在，直到 1C-4 真的用 `make start` 跑一次上傳才浮現——smoke 的
# worker fixture 當時用 `--pool solo`（為了少一層行程），於是測到的形狀與部署的形狀
# 不同，而**差異剛好就在出事的那一項**。兩邊現在一致，並由 test_dev_launcher.py 對帳。
WORKER_CMD = LOG_FORMAT=console $(UV_RUN) python -m celery -A config.celery_app worker \
	--queues etl,embedding,maintenance --pool threads --concurrency $(ETL_CONCURRENCY) --loglevel info

worker: ## 啟動背景 worker（Celery，etl + embedding + maintenance 佇列；需先 make up）
	$(WORKER_CMD)

# Beat 排程器（2A-2b）。**單一行程**：Beat 是排程的唯一發令者，跑兩份會讓每個
# 排程任務都被投遞兩次——冪等擋得住錯誤但擋不住浪費。schedule 檔放 .run/（與
# pid/log 同處，make stop 之後殘留無害）。
BEAT_CMD = LOG_FORMAT=console $(UV_RUN) python -m celery -A config.celery_app beat \
	--schedule ../.run/celerybeat-schedule --loglevel info

beat: ## 啟動 Beat 排程器（分區維護／日結對帳／chunk 清理；需先 make up）
	@mkdir -p .run
	$(BEAT_CMD)

# ── 一鍵啟停 ────────────────────────────────────────────────────
# 實作在 scripts/dev.sh：背景行程要處理 process group、重導向與 pid 檔，寫成 make
# recipe 會是一串跳脫過的 shell，改一次得重讀一次（第一版正是如此，且踩了兩個坑：
# pid 檔寫錯目錄、make 因為背景行程握著 stdout 而不退出）。理由詳見該檔開頭。
#
# **啟動指令由這裡傳入，dev.sh 不自帶**：API 用與 make dev 同一份 DEV_CMD，
# 前端用同一份 PNPM——單一來源，test_logging.py 的 DEV_RELOAD_DIRS 對帳測試
# 因此同時涵蓋前景（dev）與背景（start）兩條路，不會只守到其中一份。
#
# 前置目標的順序有意義：容器 → migration → JWT 金鑰（.NOTPARALLEL 保證序列執行）。
# 金鑰缺檔時 API 是 Fail Fast（services/identity/tokens.py），先起 API 只會拿到
# 一個看不懂的 RuntimeError。
DEV_SH = DEV_PORT=$(DEV_PORT) FE_PORT=$(FE_PORT) \
	API_CMD='$(DEV_CMD)' FE_CMD='$(PNPM) run dev --port $(FE_PORT)' \
	WORKER_CMD='$(WORKER_CMD)' BEAT_CMD='$(BEAT_CMD)' \
	bash scripts/dev.sh

start: up migrate gen-jwt-keys ## 一鍵啟動：容器 + API + 前端（背景執行，log 在 .run/）
	@$(DEV_SH) start

restart: ## 只重啟 API 與前端（跳過容器/migration/金鑰，日常快速迭代用）
	@$(DEV_SH) stop
	@$(DEV_SH) start

stop: ## 停掉 API 與前端，並關閉容器（資料卷保留）
	-@$(DEV_SH) stop
	$(COMPOSE) down

status: ## 看目前起了什麼
	@$(DEV_SH) status
	@$(COMPOSE) ps

app-logs: ## 追蹤 API 與前端的 log（背景執行時）
	@$(DEV_SH) logs

# 密碼可覆寫：`make demo-tenant DEMO_PASSWORD=...`。預設值只適用本機開發環境，
# 而那裡的 .env 本來就是 change-me-locally 等級的憑證。
# recipe 必須加 @：make 預設回顯整條指令——含密碼——正是「secrets 不進 log」要防的。
# --exist-ok：重跑視為成功，`make start && make demo-tenant` 可無腦重複執行。
demo-tenant: ## 建立本機實測用的租戶與 Owner（slug=$(DEMO_SLUG)；可重複執行）
	@$(UV_RUN) python manage.py create_tenant \
		--name "Demo" --slug "$(DEMO_SLUG)" \
		--owner-email "$(DEMO_EMAIL)" --owner-password "$(DEMO_PASSWORD)" \
		--exist-ok
	@echo "登入用：tenant_slug=$(DEMO_SLUG)  email=$(DEMO_EMAIL)"

# 平行測試（pytest-xdist）。每個 worker 走自己的 test database（`_gwN` 後綴，靠
# tests/conftest.py 的 `django_db_setup` 相依 `django_db_modify_db_settings`）。
# `auto` = 邏輯核心數；`make test PYTEST_XDIST_N=1` 會**完全關掉** xdist 而不是開一個
# worker——`-n 1` 仍走 worker 行程，pdb 與 `-s` 一樣接不到，而除錯要的是真的序列。
#
# **不加 `--reuse-db`**（試過，會壞）：transaction=True 的測試每條之後跑 Django 的
# flush（TRUNCATE），連 migration 種的權限字典與四個系統角色一起清掉，而 session
# 結束時沒有任何東西還原。留著資料庫等於把「被清空的狀態」交給下一次跑，於是
# tests/integration/test_permission_seed.py 從第二次起永遠紅——而它紅的原因看起來
# 像是 migration 寫錯。每次重建的代價實測只有 ~2s（migration 很快），遠低於這個。
#
# **不加 `--dist loadscope`**（試過，會壞）：pytest-django 會把非 transactional 的
# 測試重排到 transactional 之前，正是為了讓前者看得到 migration 種的資料。
# loadscope 整個 module 打包派發，那個全域排序就沒了——同一個 worker 可能先跑
# test_uow.py 再跑 test_permission_seed.py。預設的 `load` 依收集順序逐條派發，
# 每個 worker 拿到的子集保留相對順序，因此排序保證仍然成立。
# 跨 worker 不需要擔心：各自是不同的資料庫。
#
# **上限是 14**（不是核心數）：worker 之間靠 Redis 的邏輯 DB 分開，而 Redis 預設
# 只有 16 個（0 號留給序列跑）。超出會在 tests/conftest.py 明確 raise。
#
# 測試是 IO-bound（等 PostgreSQL 與 MinIO 的往返），所以超額訂閱有用，但報酬遞減。
# 2026-08-15 於 6 核 i5-9400F 實測 576 條：
#   序列 211s ／ n=6 76–85s（~2 核）／ n=12 71.6s（~3 核）／ n=14 67.5s
# 預設留 `auto`（= 核心數）而不是釘死 12：CI runner 的核心數不同，釘死的值在那裡
# 只會是錯的。本機想再快就 `make test PYTEST_XDIST_N=12`。
PYTEST_XDIST_N ?= auto
ifeq ($(PYTEST_XDIST_N),1)
PYTEST_PARALLEL =
else
PYTEST_PARALLEL = -n $(PYTEST_XDIST_N)
endif

test: ## 執行全部測試（需先 make up）；平行度可用 PYTEST_XDIST_N 覆寫，=1 為序列
	$(UV_RUN) pytest $(PYTEST_PARALLEL)

# 分層目標對應 02 §2 的測試四層；CI 分階段跑（unit 最快，壞掉時最好定位）。
# unit 沒有外部依賴，開 xdist 只賺行程啟動成本以外的部分——仍然值得（269 條）。
#
# **$(PYTEST_PARALLEL) 放在路徑後面，不要「整理」到前面**：
# tests/unit/test_ci_pipeline.py 展開這幾行的 Makefile 變數後，比對子字串
# `pytest tests/unit`（斷言 CI 確實跑得到 unit 層）。旗標插在中間會把那個子字串
# 切斷，於是 CI 報「缺少階段 unit 測試」——而階段其實在跑，訊息指向完全錯的方向。
# pytest 的選項放在位置參數之後同樣有效，所以這個順序沒有其他代價。
test-unit: ## 只跑 unit（無外部依賴，不需 make up）
	$(UV_RUN) pytest tests/unit $(PYTEST_PARALLEL)

test-integration: ## 只跑 integration（Repository / 基礎設施；需先 make up）
	$(UV_RUN) pytest tests/integration $(PYTEST_PARALLEL)

test-api: ## 只跑 api（權限矩陣、錯誤格式、SSE 協定；需先 make up）
	$(UV_RUN) pytest tests/api $(PYTEST_PARALLEL)

# smoke **不在 make test 裡**（pyproject 的 testpaths 排除 tests/e2e）：它要起一個
# 真的 uvicorn 子行程並連開發資料庫，前置條件比其他三層多（make up + make migrate
# + make gen-jwt-keys）。混進去會讓「單元測試」在缺任一前置時整片紅，而紅燈的原因
# 與被測的東西無關。13 §1.2：**每次任務結束必跑**，不過視同任務未完成。
smoke: ## E2E smoke suite（登入→上傳→ready→問答→引用；需先 make up / migrate / gen-jwt-keys）
	$(UV_RUN) pytest tests/e2e

# **唯一會打真 API 的目標**，而且只在人手動執行時（1C-5）。自動測試一律用假的 HTTP
# 層（CLAUDE.md），那驗得了「請求長什麼樣」，驗不了「那家真的收不收」——base_url 少一
# 個字、認證標頭格式不對、Gemini 的相容端點吃不吃 dimensions，都要真的打一次才知道。
#
# **不准接進 make test / lint / smoke 或 CI**：CI 會開始花錢，且會因為別人的服務中斷
# 而紅——那種紅燈與改動無關，久了就沒有人看紅燈了。
# 守門：tests/unit/test_dev_launcher.py::TestProviderVerification
PROVIDER ?= gemini
# embedding（預設）或 chat。chat 走 /chat/completions 的串流，驗的是 SSE 格式、
# `[DONE]` 與 `stream_options.include_usage`——假的 HTTP 層之下那些全是我們自己的預期值。
CAPABILITY ?= embedding

verify-provider: ## 手動打一次真的 API（PROVIDER= 指定廠商、CAPABILITY=embedding|chat）
	$(UV_RUN) python scripts/verify_provider.py --provider $(PROVIDER) --capability $(CAPABILITY)

# 只挑 test_infra_*.py（-k 會比對 module 名，不用 shell glob——glob 會在 repo 根展開，
# 而 pytest 的工作目錄是 backend/，路徑對不上）。與 test-integration 的差別在此：
# 那個目標跑整個 integration 層（含 bridge / tenant scope / db timeout）。
verify-infra: ## 只跑基礎設施驗收（extension / collation / Redis / MinIO / secrets）
	$(UV_RUN) pytest tests/integration -k infra

# ── 前端與 OpenAPI codegen（03、09 §4）─────────────────────────────
# Node 由 nvm 管（~/.nvm，見 README 前置需求）；版本依 ADR-007 為 22 LTS，
# frontend/package.json 的 engines 與 packageManager 兩個欄位釘住。

fe-install: ## 安裝前端相依（照 lockfile，不會就地更新）
	# --frozen-lockfile：不帶時 pnpm 會就地改寫 pnpm-lock.yaml，於是 CI 裝到的版本
	# 可能與任何人本機裝的都不同，而 lockfile 的改動不會出現在 PR 裡。
	$(PNPM) install --frozen-lockfile

fe-lint: ## 前端 eslint + vue-tsc（型別）
	# 兩條都要跑：eslint 不做型別推導，vue-tsc 才是 TS strict 的守門，
	# 其中一個被拿掉時另一個照樣全綠。
	$(PNPM) run lint
	$(PNPM) run typecheck

fe-test: ## 前端 vitest（含 tests/types 的型別層）
	$(PNPM) run test

fe-build: ## 前端 production build（vue-tsc + vite build）
	$(PNPM) run build

fe-dev: ## 前端開發伺服器（http://localhost:5173，/api 由 proxy 轉給 make dev）
	$(PNPM) run dev

openapi: ## 匯出 OpenAPI 契約到 $(CONTRACT)（單一事實來源仍是 FastAPI）
	$(UV_RUN) python scripts/export_openapi.py

gen-api: ## 由契約重新產生前端 typed client（禁止手改產物）
	# 產生器的 --default-non-nullable false 是必要的：openapi-typescript 預設把
	# 「有 default 值的欄位」當成回應中必定存在，但本 API 的錯誤回應是手工組出來的
	# （api/main.py 的 problem_response），code / errors / details 不存在時整個鍵就不在。
	# 不關掉的話前端型別會說 `problem.code` 必有，實際上拿到 undefined。
	$(PNPM) run gen:api

# 兩段鏈一起驗（03 §3.1）：契約 == app、generated == 契約。
# 前置目標的順序有意義：先重新匯出，再重新產生，最後看工作區乾不乾淨。
openapi-check: openapi gen-api ## 驗證契約與 generated client 未過期（CI 用）
	git diff --exit-code -- $(CONTRACT) $(GENERATED)
	# 上一行看不到**未追蹤**的檔案：第一次產生 generated 卻忘了 git add 時，
	# diff 是空的、CI 全綠，而 clone 下來的人根本沒有那個目錄。
	@test -z "$$(git status --porcelain -- $(CONTRACT) $(GENERATED))" || { \
		echo "契約或 generated client 有未提交的變更："; \
		git status --short -- $(CONTRACT) $(GENERATED); \
		exit 1; }

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
# ruff 帶 --no-cache：cache 以檔案 metadata 為鍵，曾對 test_rag_params.py 沿用舊的
# 「通過」判定——本機連續綠、CI（無 cache）連紅四次（run 57–60，2026-08-19 查明）。
# 代價實測 <2s；「lint 的結論可信」值這個價。mypy 的 cache 是語意級的，不在此列。
lint-backend: lock-check ## 只跑後端：uv.lock 檢查 + ruff + mypy + import-linter（分層依賴強制）
	$(UV) run ruff check --no-cache .
	$(UV) run ruff format --check --no-cache .
	$(UV_RUN) mypy .
	$(UV) run lint-imports

# `lint` 是**兩端都跑**的那一個，而後端仍留一個獨立目標（lint-backend）：CI 的 quality
# job 沒有 Node 也沒有 node_modules（前端另有 job，見 ci.yml），在那裡跑 fe-lint 只會
# 得到 `eslint: not found`——一個與程式碼品質完全無關的紅燈。
#
# 前後端分成兩個目標、卻只有一個總入口，是因為漏跑的方向是單向的：本機習慣打 `make lint`
# 的人不會記得另外補一次 `make fe-lint`，於是前端的型別錯誤要等到推上去才由 CI 發現。
# 反過來（CI 少跑前端）不會發生——那是 workflow 裡獨立的一個 job。
#
# 前置目標的順序有意義：後端先跑。改後端的次數遠多於前端，先紅的那一端應該是常改的那端。
lint: lint-backend fe-lint ## 後端 + 前端全部靜態檢查（前端需先 make fe-install）

# push 之後必跑（CLAUDE.md Git 規則）。存在的理由：CI 曾連紅四次（run 57–60）無人
# 察覺——test_ci_pipeline.py 只防「步驟缺漏」，防不了「內容真的紅」，而 GitHub 的
# email 通知太容易被淹沒。跑在 in_progress 時會輪詢到完成為止（上限 20 分鐘）。
# 不依賴 gh CLI：repo 是公開的，走匿名 REST API 即可。
ci-status: ## 查最近 push 的 CI 結果（進行中會輪詢至完成；紅燈時列出失敗的 job/step）
	python3 scripts/ci_status.py

# 負載產生端（locust）於 1A-5 隨 spike 面移除，理由見檔案開頭。留下的是**伺服器端**
# 的延遲分析：它讀 access log 的 duration_ms（伺服器行程內量的），與用什麼工具打
# 流量無關，換成 k6、ab、或手動打都適用。
#
# 為什麼伺服器端數字非有不可：客戶端量到的 p95 含負載產生器自己在同一台機器上排隊
# 的時間。兩個數字對照才分得出「系統慢」與「壓測工具跟不上」——2026-08-05 就是靠
# 這個對照才發現 200 併發下 CPU 還有 27.5% idle 而 p95 破秒（11 §1.4）。
#
# --path / --last-seconds 是必填參數：前者指定要分析哪條路徑（沒有預設值可言，端點
# 隨工作包而變），後者篩出最後一輪（log 持續附加，不篩會拿到多輪的混合值）。
loadtest-report: ## 從 $(API_LOG) 算伺服器端延遲分位數。用法：make loadtest-report ARGS="--path /api/v1/users --last-seconds 70"
	$(UV) run python loadtest/analyze_access_log.py $(API_LOG) $(ARGS)

clean: ## 停止並刪除資料卷（會清空資料庫、Redis、MinIO）
	$(COMPOSE) down -v

; PgBouncer 設定樣板 —— 由 compose 的 pgbouncer-config 服務渲染成實檔。
;
; 佔位符 __DB_USER__ / __DB_PASSWORD__ / __DB_NAME__ 於容器啟動時以 .env 的值取代。
; 為什麼要這層樣板：PgBouncer 不支援設定檔內的環境變數插值，而 [databases] 這行
; 必須帶密碼（原因見下），直接寫進版控就是把密碼提交上去。
;
; 為什麼不用 edoburu image 的自動生成：它的 entrypoint 會產出
;     lumina = host=postgres port=5432 auth_user=lumina
; 也就是走 auth_query 模式——PgBouncer 去查 PG 的 pg_shadow 取密碼。但 PG16 的
; pg_shadow 存的是 SCRAM verifier，PgBouncer 拿 verifier 只能驗證 client，
; 無法反過來用它登入 PostgreSQL（已知限制）。結果是 server 端 login failed，
; 錯誤訊息卻顯示成 client 密碼錯誤，很難查。

[databases]
__DB_NAME__ = host=postgres port=5432 dbname=__DB_NAME__ user=__DB_USER__ password=__DB_PASSWORD__

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 5432
unix_socket_dir =

auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt

; 管理與觀測分開：
;   admin_users 能下 PAUSE / KILL / RELOAD / SHUTDOWN——足以讓全站連不上。
;   stats_users 只能跑 SHOW（POOLS / STATS / CLIENTS…），純唯讀。
; 應用帳號的密碼會出現在每個部署單元的環境變數裡，流通範圍遠大於運維憑證，
; 所以它只拿 stats。壓測看 `SHOW POOLS` 因此完全不受影響（見下方指令）。
;
; __PGBOUNCER_ADMIN_USER__ 不需要是 PostgreSQL 的 role：管理主控台認的是
; userlist.txt，連的是虛擬的 `pgbouncer` database，不會轉往 PG。
admin_users = __PGBOUNCER_ADMIN_USER__
stats_users = __DB_USER__

; 05 §5.5：transaction pooling
pool_mode = transaction
max_client_conn = 200
default_pool_size = 20

; psycopg3 會送這些 startup 參數，transaction mode 下必須忽略否則連線被拒。
; ⚠️ options 被忽略 = 任何想靠 startup 參數帶進來的設定（例如 statement_timeout）
; 都會被靜默丟棄。statement_timeout 因此設在 role 上，由 `make db-timeouts` 套用。
ignore_startup_parameters = extra_float_digits,options

; CLAUDE.md：所有對外呼叫必有 timeout。
; 預設 120 秒——pool 滿載時 client 會在 PgBouncer 排隊到兩分鐘，且那兩分鐘會被
; 記成「一次很慢的成功請求」而非錯誤，p95/p99 直接被這條尾巴汙染。
;
; default_pool_size(20) < 峰值需求(4 workers × 8 threadpool = 32) 是**刻意**的
; ——排隊行為本身是 B 組要量的對象。10 秒因此是安全網而不是量測干擾：
; 排隊時間 ≈ 佇列深度 × 單次交易時間，走索引取 20 列不可能累積到秒級，
; 正常排隊仍會如實記成延遲讓你觀察，只有真的失控時才轉成錯誤。
;
; 當下佇列狀態看 SHOW POOLS 的 cl_waiting / maxwait，比 rps 更能指出瓶頸。
query_wait_timeout = 10

; 壓測要看 pool 狀態時：psql -p 16432 -U lumina pgbouncer -c "SHOW POOLS;"
; （SHOW 走 stats_users，用應用帳號即可；PAUSE / KILL 這類要改用 admin_users 的帳號）
log_connections = 0
log_disconnections = 0
stats_period = 60

; client → PgBouncer 的認證清單樣板；由 compose 的 pgbouncer-config 服務渲染。
; 密碼以明文存放於容器內的具名 volume（PgBouncer 以此對 client 做 SCRAM 驗證）——
; 不進版控，值來自 .env。
; 第一個是應用帳號（ini 內為 stats_users），第二個是管理帳號（admin_users）。
; 兩者的帳號與密碼都不得相同，否則分離形同虛設——render.sh 會擋。
"__DB_USER__" "__DB_PASSWORD__"
"__PGBOUNCER_ADMIN_USER__" "__PGBOUNCER_ADMIN_PASSWORD__"

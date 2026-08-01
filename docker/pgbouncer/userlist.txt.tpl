; client → PgBouncer 的認證清單樣板；由 compose 的 pgbouncer-config 服務渲染。
; 密碼以明文存放於容器內的具名 volume（PgBouncer 以此對 client 做 SCRAM 驗證）——
; 不進版控，值來自 .env。
"__DB_USER__" "__DB_PASSWORD__"

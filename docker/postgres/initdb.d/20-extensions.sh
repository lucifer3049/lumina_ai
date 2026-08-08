#!/bin/bash
# pgvector 與 pgroonga —— 由 **superuser 在 initdb 階段**建立（05 §3.2、§5.3）。
#
# **這推翻了 Phase 0 的決定**（原本一律走 Django migration，見
# apps/platform/migrations/0001_extensions.py）。原因是角色拆分（13 §3.1）之後
# migration 不再以 superuser 執行，而這兩個 extension 都**不是 trusted**
# （image 內的 vector.control / pgroonga.control 都沒有 `trusted = true`），
# 於是 owner 角色下 `CREATE EXTENSION` 會直接
# `permission denied to create extension "vector"`。
#
# 三條路只有這條可行：
#   1. 給 owner superuser —— 等於廢掉整個角色拆分（superuser 豁免 RLS）。
#   2. 自行改 control 檔加 trusted —— 動 image 內第三方套件的檔案，升級即失效，
#      且 trusted 的語意是「這個 extension 不會讓非特權使用者提權」，那是上游的
#      判斷，不是我們該替它做的。
#   3. superuser 在 initdb 建好，migration 變成 no-op（本檔）。
#
# 原決定的理由——「initdb.d 不會套用到 pytest 另建的 test database」——由對
# **template1** 也建一次解決：`CREATE DATABASE` 預設從 template1 複製，於是
# test_${DB_NAME} 天生就帶著這兩個 extension。
#
# migration 保留不動且仍然安全：Django 的 `CreateExtension` 會先查 `pg_extension`，
# 已存在就整個跳過，不會以 owner 身分去撞權限。它因此仍是「這個專案需要哪些
# extension」的單一宣告處，也仍然涵蓋不經本腳本建立的資料庫。
set -euo pipefail

for database in "${POSTGRES_DB}" template1; do
    psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${database}" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgroonga;
SQL
done

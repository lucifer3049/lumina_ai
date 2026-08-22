#!/bin/bash
# 角色拆分（13 §3.1 的 1A-P1、05 §5.1）—— 只在資料卷**首次初始化**時執行。
#
# 三個角色，職責完全不重疊：
#   ${POSTGRES_USER}    initdb superuser。建完下面兩個角色之後就不再有人用它
#                       （除了 `make psql` 這種 break-glass 途徑）。
#   ${DB_ADMIN_USER}    schema owner：Django migration、pytest 建 test database、
#                       維運腳本。非 superuser——superuser 會豁免所有權限檢查，
#                       本機就測不出 production 會缺的授權。
#   ${DB_USER}          應用執行期唯一連線。非 superuser、非 owner、非 BYPASSRLS、
#                       且**不是** owner 的成員（成員可 SET ROLE 取得 owner 豁免）。
#
# 為什麼放 initdb.d，而 extension 不放（見 Dockerfile 末段的相反決定）：
# 角色是 **cluster 級**物件，建一次全庫適用，pytest 另建的 test database 照樣看得到；
# extension 是 **database 級**的，initdb.d 建的只存在於 ${POSTGRES_DB}，
# test database 不會有——那正是「本機綠、CI 紅」的來源。兩者放置位置的差異出自
# 這個作用域差異，不是風格。
#
# 而 schema 授權與 default privileges 是 database 級的，所以下面對 **template1**
# 也跑一次：`CREATE DATABASE` 預設從 template1 複製，於是 pytest 建出來的
# test_${DB_NAME} 自動帶著同一套授權。少了這步，測試會在「migration 建好表、
# 應用角色卻無權 SELECT」的狀態下失敗，而錯誤訊息指向 permission denied，
# 看不出根因是 default privileges 沒有跨資料庫繼承。
set -euo pipefail

: "${DB_USER:?10-roles.sh 需要 DB_USER}"
: "${DB_PASSWORD:?10-roles.sh 需要 DB_PASSWORD}"
: "${DB_ADMIN_USER:?10-roles.sh 需要 DB_ADMIN_USER}"
: "${DB_ADMIN_PASSWORD:?10-roles.sh 需要 DB_ADMIN_PASSWORD}"

# 同名等於沒有拆分（應用連的就是 owner），且沒有任何症狀。
if [ "${DB_USER}" = "${DB_ADMIN_USER}" ] || [ "${DB_USER}" = "${POSTGRES_USER}" ]; then
    echo "10-roles.sh: DB_USER 不可與 DB_ADMIN_USER / POSTGRES_USER 相同" >&2
    exit 1
fi

# 帳號與密碼一律經 psql 變數傳入：:"var" 走識別字引號、:'var' 走字面值引號，
# 兩者都由 psql 正確跳脫。字串拼接會在密碼含 ' 或 \ 時產生語法錯誤或錯誤密碼。
psql_run() {
    psql -v ON_ERROR_STOP=1 \
        --username "${POSTGRES_USER}" \
        --dbname "$1" \
        -v app_user="${DB_USER}" \
        -v app_password="${DB_PASSWORD}" \
        -v admin_user="${DB_ADMIN_USER}" \
        -v admin_password="${DB_ADMIN_PASSWORD}" \
        -v db_name="${POSTGRES_DB}"
}

# ── cluster 級：兩個角色 ────────────────────────────────────────
psql_run "${POSTGRES_DB}" <<'SQL'
-- CREATEDB 是 pytest 的硬需求（13 §3.1 的 1A-P2）；其餘一律否定式顯式寫出，
-- 因為 PostgreSQL 的預設是「沒有」，而顯式的 NOSUPERUSER / NOBYPASSRLS 讓
-- 「誰改過這裡」在 diff 上看得見。
CREATE ROLE :"admin_user" LOGIN PASSWORD :'admin_password'
    CREATEDB NOSUPERUSER NOCREATEROLE NOBYPASSRLS NOREPLICATION;

-- NOINHERIT：即使日後誰把它加進某個群組，權限也不會自動生效（要顯式 SET ROLE），
-- 於是提權在 diff 與稽核紀錄上留得下痕跡。
CREATE ROLE :"app_user" LOGIN PASSWORD :'app_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION NOINHERIT;

ALTER DATABASE :"db_name" OWNER TO :"admin_user";
SQL

# ── database 級：schema 授權與 default privileges ───────────────
# template1 也跑：新建的資料庫（含 pytest 的 test database）由它複製而來。
for database in "${POSTGRES_DB}" template1; do
    psql_run "${database}" <<'SQL'
ALTER SCHEMA public OWNER TO :"admin_user";

-- PG15 起 public schema 對 PUBLIC 已無 CREATE，這行是把「不可建物件」寫死而不是
-- 依賴版本預設：應用角色一旦能建表，那張表的 owner 就是它自己，天然豁免 policy。
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"app_user";

-- migration 之後才存在的表也要能被應用讀寫。沒有這兩行的話，每加一張表就要
-- 手動 GRANT 一次，而漏掉的症狀是執行期 permission denied（測試不一定覆蓋到）。
--
-- **這是給業務表的一次性設定，不分表**。平台級的表（全域權限字典、登入路由表）
-- 的寫入權由 `apps/identity/migrations/0012_platform_table_grants.py` 事後收回
-- ——它們在這支腳本跑的時候還不存在（表由 migration 建立），而 default privileges
-- 也無法針對個別表例外。
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_user" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_user" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"app_user";
SQL
done

#!/bin/sh
# 把 pgbouncer 的樣板渲染成實檔（密碼由環境變數注入，不進版控）。
#
# **為什麼不用 sed**：`sed "s|__DB_PASSWORD__|$DB_PASSWORD|"` 會把密碼當成
# 替換運算式的一部分來解讀：
#   - 密碼含 `&` → sed 把它展開成「整段匹配文字」，產出的是**錯誤密碼且不報錯**，
#     症狀變成 pgbouncer 認證失敗，指向完全錯誤的原因。
#   - 密碼含 `|` → `sed: bad option in substitution expression`，容器直接掛。
#   - 密碼含 `\` → 被當跳脫字元吃掉。
# awk 的 `gsub()` 有同樣的 `&` 問題，所以這裡用 index/substr 做**字面**取代，
# 替換內容完全不經任何運算式解讀。
#
# 渲染出兩個帳號（tests/unit/test_pgbouncer_render.py 驗證兩者不得相同）：
#   DB_USER            應用帳號，在 ini 內是 stats_users（只能跑 SHOW）
#   PGBOUNCER_ADMIN_*  管理帳號，在 ini 內是 admin_users（PAUSE / KILL / RELOAD）
# 管理帳號不需要存在於 PostgreSQL——PgBouncer 的管理主控台認的是 userlist.txt，
# 連的是虛擬的 `pgbouncer` database。
set -eu

: "${DB_USER:?render.sh 需要 DB_USER}"
: "${DB_PASSWORD:?render.sh 需要 DB_PASSWORD}"
: "${DB_NAME:?render.sh 需要 DB_NAME}"
: "${PGBOUNCER_ADMIN_USER:?render.sh 需要 PGBOUNCER_ADMIN_USER}"
: "${PGBOUNCER_ADMIN_PASSWORD:?render.sh 需要 PGBOUNCER_ADMIN_PASSWORD}"

# 兩個帳號同名等於沒有分離（應用密碼照樣登得進管理主控台）。這裡擋掉而不是只靠
# 單元測試：測試驗的是樣板與腳本，擋不住有人把 .env 的兩個值設成一樣。
if [ "${DB_USER}" = "${PGBOUNCER_ADMIN_USER}" ]; then
    echo "render.sh: PGBOUNCER_ADMIN_USER 不可與 DB_USER 相同（$DB_USER）" >&2
    exit 1
fi
if [ "${DB_PASSWORD}" = "${PGBOUNCER_ADMIN_PASSWORD}" ]; then
    echo "render.sh: PGBOUNCER_ADMIN_PASSWORD 不可與 DB_PASSWORD 相同" >&2
    exit 1
fi

TPL_DIR="${TPL_DIR:-/tpl}"
OUT_DIR="${OUT_DIR:-/out}"

for name in pgbouncer.ini userlist.txt; do
    # 值一律經 ENVIRON 取得，不用 `awk -v`：-v 的賦值會**處理跳脫序列**，
    # 密碼裡的 `\s`、`\"` 會被吃掉，同樣是「靜默產出錯誤密碼」。
    awk '
        BEGIN {
            user = ENVIRON["DB_USER"]
            password = ENVIRON["DB_PASSWORD"]
            dbname = ENVIRON["DB_NAME"]
            admin_user = ENVIRON["PGBOUNCER_ADMIN_USER"]
            admin_password = ENVIRON["PGBOUNCER_ADMIN_PASSWORD"]
        }
        # 字面取代：不使用 gsub/sub，避免替換字串中的 & 被展開成匹配內容
        function replace(subject, from, to,    out, pos) {
            out = ""
            while ((pos = index(subject, from)) > 0) {
                out = out substr(subject, 1, pos - 1) to
                subject = substr(subject, pos + length(from))
            }
            return out subject
        }
        {
            line = replace($0, "__DB_USER__", user)
            line = replace(line, "__DB_PASSWORD__", password)
            line = replace(line, "__DB_NAME__", dbname)
            line = replace(line, "__PGBOUNCER_ADMIN_USER__", admin_user)
            line = replace(line, "__PGBOUNCER_ADMIN_PASSWORD__", admin_password)
            print line
        }
    ' "${TPL_DIR}/${name}.tpl" > "${OUT_DIR}/${name}"
    chmod 644 "${OUT_DIR}/${name}"
done

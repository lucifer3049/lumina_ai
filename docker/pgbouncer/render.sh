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
set -eu

: "${DB_USER:?render.sh 需要 DB_USER}"
: "${DB_PASSWORD:?render.sh 需要 DB_PASSWORD}"
: "${DB_NAME:?render.sh 需要 DB_NAME}"

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
            print line
        }
    ' "${TPL_DIR}/${name}.tpl" > "${OUT_DIR}/${name}"
    chmod 644 "${OUT_DIR}/${name}"
done

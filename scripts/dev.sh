#!/usr/bin/env bash
# 本機開發環境的啟停 —— `make start` / `make stop` / `make status` 的實作。
#
# **職責分工**：啟動「什麼指令」由 Makefile 決定（API_CMD / FE_CMD 經環境變數傳入；
# API 與 make dev 共用同一份 DEV_CMD，tests/unit/test_logging.py 的 DEV_RELOAD_DIRS
# 對帳測試因此同時涵蓋前景與背景兩條路）。本腳本只負責「怎麼跑」：process group、
# pid 檔、重導向、就緒檢查。
#
# **幾個必須這樣做的細節**：
#
# 1. `setsid`：讓每個服務自成一個 session 與 process group（PGID == PID）。停止時
#    以 `kill -TERM -- -<pid>` 送給整個 group，才收得掉 uvicorn --reload 的 worker
#    與 pnpm 底下的 vite。少了它，那些子行程會變成孤兒繼續佔著 8000 / 5173。
#
# 2. `< /dev/null` 與輸出重導向：背景行程若還握著呼叫端的 stdout，`make start`
#    永遠不會結束——管線的另一端等不到 EOF。
#
# 3. 指令以 positional args 傳進 bash -c，不做字串拼接：拼接需要對單引號做多層
#    跳脫，而那個跳脫在雙引號層內展開錯誤（實測：指令含 ' 時內層 bash 直接
#    parse error）。positional args 沒有任何一層需要跳脫。
#
# 4. `flock`：start / stop 全程持鎖——兩個終端同時操作不會雙重啟動、不會互踩
#    pid 檔；start_one 也會等 pid 檔落地才回傳，start 後立刻 stop 不再撲空。
#
# 5. 存活檢查驗整個 process group（kill -0 -- -pid）而非只驗 leader：leader 被
#    OOM killer 收掉而 worker 還活著時，只驗 leader 會把活著的服務當成已停止，
#    跳過 group kill、留下佔著埠的孤兒。
#
# pid 與 log 落在 repo 根的 .run/（已 gitignore）。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${REPO_ROOT}/.run"

API_PORT="${DEV_PORT:-8000}"
FE_PORT="${FE_PORT:-5173}"

# 啟動指令一律由 Makefile 傳入（單一來源）；直接執行本腳本沒有意義。
API_CMD="${API_CMD:?請經 make start 呼叫（API_CMD 未設定）}"
FE_CMD="${FE_CMD:?請經 make start 呼叫（FE_CMD 未設定）}"
# ETL worker（1B-6）。沒有它的話上傳的文件會停在 uploaded——訊息進了佇列卻沒有人
# 處理，而 API 側完全看不出來（回應是 201，狀態欄看起來只是「還在處理」）。
WORKER_CMD="${WORKER_CMD:?請經 make start 呼叫（WORKER_CMD 未設定）}"

pid_file() { echo "${RUN_DIR}/$1.pid"; }
log_file() { echo "${RUN_DIR}/$1.log"; }

group_alive() { kill -0 -- "-$1" 2>/dev/null; }

is_running() {
  local file
  file="$(pid_file "$1")"
  [[ -s "${file}" ]] && group_alive "$(cat "${file}")"
}

acquire_lock() {
  exec 9>"${RUN_DIR}/.lock"
  flock 9
}

ensure_port_free() {
  # wait_for 只探測埠：若埠上已有別的 server（先前的孤兒、前景 make dev），
  # 新行程會 bind 失敗立刻死掉，而探測卻對舊 server 成功——誤報「就緒」。
  # 所以啟動前先驗埠是空的，把「埠被誰佔住」在第一時間講清楚。
  local name="$1" port="$2"
  is_running "${name}" && return 0
  if (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then
    echo "埠 ${port} 已被其他行程佔用（不是本腳本管理的 ${name}）。" >&2
    echo "先找出並停掉它（ss -ltnp | grep ${port}），否則新行程只會啟動失敗。" >&2
    return 1
  fi
}

start_one() {
  local name="$1" command="$2" file
  file="$(pid_file "${name}")"

  if is_running "${name}"; then
    echo "${name} 已在執行（pid $(cat "${file}")）"
    return
  fi
  rm -f "${file}"

  # 子行程自己寫 pid：setsid 可能 fork，外層的 $! 不保證是 session leader。
  #
  # `9>&-` 關掉繼承來的鎖 fd（1B-6 發現）。flock 綁的是 open file description，而
  # fd 會被子行程繼承——服務跑起來之後那個 description 永遠有人持有，於是**下一次
  # `make stop` 會永遠卡在 acquire_lock**，症狀是指令沒有輸出也不結束。
  setsid bash -c 'echo $$ > "$1"; cd "$2"; eval "$3"' _ \
    "${file}" "${REPO_ROOT}" "${command}" \
    > "$(log_file "${name}")" 2>&1 < /dev/null 9>&- &

  # 等 pid 檔落地再回傳：否則 start 後立刻 stop/status 會看不到行程，留下孤兒。
  for _ in {1..50}; do
    [[ -s "${file}" ]] && break
    sleep 0.1
  done
  if [[ ! -s "${file}" ]]; then
    echo "${name} 啟動失敗（pid 檔未出現），$(log_file "${name}") 最後 20 行：" >&2
    tail -20 "$(log_file "${name}")" >&2 || true
    return 1
  fi
  echo "${name} 啟動中（pid $(cat "${file}")）"
}

stop_one() {
  local name="$1" file pid
  file="$(pid_file "${name}")"
  [[ -s "${file}" ]] || { rm -f "${file}"; return 0; }

  pid="$(cat "${file}")"
  if group_alive "${pid}"; then
    # 負號 = 整個 process group。先 TERM 給它時間收尾，還活著才 KILL。
    kill -TERM -- "-${pid}" 2>/dev/null || true
    for _ in {1..20}; do
      group_alive "${pid}" || break
      sleep 0.25
    done
    if group_alive "${pid}"; then
      kill -KILL -- "-${pid}" 2>/dev/null || true
    fi
    echo "已停止 ${name}（pid ${pid}）"
  fi
  rm -f "${file}"
}

wait_for() {
  # 等到真的可用再回報成功：背景啟動失敗（缺 .env、金鑰缺檔、沒裝 node_modules）
  # 時 pid 檔照樣建得起來，只印「啟動中」會讓人以為好了，然後在瀏覽器看到連不上。
  # API 與前端都要等——只等 API 的話，vite 秒死也照樣印成功 banner。
  local name="$1" url="$2"
  for _ in {1..60}; do
    if curl -sf --max-time 2 "${url}" > /dev/null 2>&1; then
      echo "${name} 就緒"
      return 0
    fi
    if ! is_running "${name}"; then
      echo "${name} 啟動失敗，$(log_file "${name}") 最後 20 行：" >&2
      tail -20 "$(log_file "${name}")" >&2 || true
      return 1
    fi
    sleep 0.5
  done
  echo "${name} 在 30 秒內沒有就緒，log：$(log_file "${name}")" >&2
  return 1
}

wait_alive() {
  # 給服務幾秒鐘暴露啟動期的失敗（import 錯誤、連不上 broker），再確認它還在。
  # 沒有這段的話，「起來後兩秒就死」與「正常執行」在 start 的輸出裡完全一樣。
  local name="$1"
  sleep 3
  if is_running "${name}"; then
    echo "${name} 就緒"
    return 0
  fi
  echo "${name} 啟動失敗，$(log_file "${name}") 最後 20 行：" >&2
  tail -20 "$(log_file "${name}")" >&2 || true
  return 1
}

mkdir -p "${RUN_DIR}"

case "${1:-}" in
  start)
    acquire_lock
    ensure_port_free api "${API_PORT}"
    ensure_port_free frontend "${FE_PORT}"
    # vite 的 /api proxy 要跟著 API 埠走（frontend/vite.config.ts 讀這個變數），
    # 否則 make start DEV_PORT=8001 會得到一個每個請求都 ECONNREFUSED 的前端。
    export VITE_DEV_API_TARGET="${VITE_DEV_API_TARGET:-http://127.0.0.1:${API_PORT}}"
    start_one api "${API_CMD}"
    start_one frontend "${FE_CMD}"
    start_one worker "${WORKER_CMD}"
    wait_for api "http://127.0.0.1:${API_PORT}/openapi.json"
    wait_for frontend "http://127.0.0.1:${FE_PORT}/"
    # worker 沒有埠可以探測，改驗「起來之後還活著」：缺 .env、broker 連不上這類
    # 失敗會讓它在數秒內退出，而 pid 檔照樣建得起來。
    wait_alive worker
    cat <<EOF

  前端      http://127.0.0.1:${FE_PORT}
  API 文件  http://127.0.0.1:${API_PORT}/docs
  log       make app-logs        停止  make stop
  測試帳號  make demo-tenant（可重複執行）
EOF
    ;;
  stop)
    acquire_lock
    stop_one api
    stop_one frontend
    stop_one worker
    ;;
  status)
    for name in api frontend worker; do
      if is_running "${name}"; then
        echo "${name}     執行中（pid $(cat "$(pid_file "${name}")")）"
      else
        echo "${name}     未執行"
      fi
    done
    ;;
  logs)
    touch "$(log_file api)" "$(log_file frontend)" "$(log_file worker)"
    tail -f "$(log_file api)" "$(log_file frontend)" "$(log_file worker)"
    ;;
  *)
    echo "用法：$0 {start|stop|status|logs}" >&2
    exit 2
    ;;
esac

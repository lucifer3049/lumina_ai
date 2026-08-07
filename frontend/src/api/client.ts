/**
 * HTTP 傳輸層——前端唯一碰網路的地方（03 §2）。
 *
 * Phase 0 只做四件事：接 baseURL、逾時、把後端的 Problem Details（09 §1.3）
 * 正規化成一種例外、以及**永遠不吞錯**。401 refresh（03 §3.3）屬於 1A 認證
 * 工作包，SSE 走 `api/sse.ts`（1D），兩者刻意不在這裡預先鋪。
 *
 * 「只有一種例外」是這一層的核心價值：呼叫端不需要分辨 TypeError（網路斷）、
 * AbortError（逾時）與 HTTP 錯誤——它們在畫面上都是「這次操作失敗了」，
 * 差別只在 `code`。
 */

import type { ProblemDetail } from '@/types/models'

/** 逾時預算。值出自 11 §4.1 Timeout 全域字典的「HTTP 對外 15s」。 */
export const API_TIMEOUT_MS = 15_000

/** 傳輸層自己產生的錯誤碼——**不屬於** 09 附錄 A 的契約字典，後端不會回這些值。 */
export const CLIENT_ERROR_CODES = {
  network: 'NETWORK_ERROR',
  timeout: 'TIMEOUT',
} as const

/**
 * 解析中的 problem body（09 §1.3）。
 *
 * 型別**綁 generated 的 `ProblemDetail`**，不自己重宣告一份。前一版是一個所有欄位
 * 都 `?: unknown` 的本地 interface，結構上與任何 JSON 相容——後端把 `request_id`
 * 改名時，`openapi.json` → `generated/schema.ts` → `types/models.ts` 整條鏈都會更新，
 * 只有這裡不會：`vue-tsc` 綠、`make openapi-check` 綠，而 `ApiError.requestId` 對每個
 * 錯誤回應都靜默變成 null。那是使用者唯一能回報、也是唯一能撈到 log 的線索。
 *
 * `Partial<>` 是必要的：body 來自網路，執行期不保證符合契約（標頭說 problem+json
 * 但內容是代理吐的東西），所以解析仍要逐欄位防禦（見 `asString`）。
 */
type ProblemBody = Partial<ProblemDetail>

export interface RequestOptions extends Omit<RequestInit, 'signal'> {
  /** 覆寫逾時（毫秒）。長任務請走 202 + job 輪詢（09 §3.3），不要無限放大這個值。 */
  timeoutMs?: number
  signal?: AbortSignal
}

/**
 * 所有請求失敗的唯一例外型別。
 *
 * `code` 可能為 null：後端對 405 這類沒有對應契約碼的回應走 `about:blank`
 * （見 backend/api/main.py 的 problem_response）。前端**不從 status 反推**一個
 * 看起來像 code 的字串——那會讓 UI 開始判斷契約裡不存在的值。
 */
export class ApiError extends Error {
  readonly status: number
  readonly code: string | null
  readonly title: string
  readonly detail: string
  readonly requestId: string | null

  constructor(init: {
    status: number
    code: string | null
    title: string
    detail: string
    requestId: string | null
    cause?: unknown
  }) {
    super(init.detail, init.cause === undefined ? undefined : { cause: init.cause })
    this.name = 'ApiError'
    this.status = init.status
    this.code = init.code
    this.title = init.title
    this.detail = init.detail
    this.requestId = init.requestId
  }
}

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? ''

const asString = (value: unknown): string | null => (typeof value === 'string' ? value : null)

/**
 * 把失敗的回應轉成 ApiError。
 *
 * body 不是 problem+json 時（反向代理吐的 HTML、gateway 的純文字）**不把內容
 * 帶進 detail**：那既難讀，也可能夾帶內部主機名之類的資訊。改用狀態碼敘述，
 * 追查靠 requestId。
 */
async function toApiError(response: Response): Promise<ApiError> {
  const headerRequestId = response.headers.get('X-Request-Id')
  const fallback = new ApiError({
    status: response.status,
    code: null,
    title: response.statusText || `HTTP ${response.status}`,
    detail: response.statusText || `請求失敗（HTTP ${response.status}）`,
    requestId: headerRequestId,
  })

  if (!response.headers.get('Content-Type')?.includes('problem+json')) {
    return fallback
  }

  let body: ProblemBody
  try {
    body = (await response.json()) as ProblemBody
  } catch {
    // 標頭說是 problem+json 但 body 解不開：只能退回狀態碼版本。
    // 這裡吞掉解析錯誤是刻意的——真正該讓呼叫端看到的是 HTTP 失敗本身。
    return fallback
  }

  return new ApiError({
    status: response.status,
    code: asString(body.code),
    title: asString(body.title) ?? fallback.title,
    detail: asString(body.detail) ?? fallback.detail,
    requestId: asString(body.request_id) ?? headerRequestId,
  })
}

/**
 * 發一個請求並回傳解析後的 body（204 / 空 body 回 null）。
 *
 * 逾時用 AbortController 而非只是 reject：只 reject 的話連線會留著直到伺服器
 * 回應，使用者反覆切分頁時會累積。
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { timeoutMs = API_TIMEOUT_MS, signal: callerSignal, headers, ...init } = options

  const controller = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)

  // 呼叫端自己的 signal（元件卸載時取消）必須保留，不能被逾時用的那個蓋掉。
  const signal = callerSignal
    ? AbortSignal.any([callerSignal, controller.signal])
    : controller.signal

  let response: Response
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: { Accept: 'application/json', ...headers },
      signal,
    })
  } catch (cause) {
    if (timedOut) {
      throw new ApiError({
        status: 0,
        code: CLIENT_ERROR_CODES.timeout,
        title: '請求逾時',
        detail: `請求超過 ${timeoutMs}ms 未完成`,
        requestId: null,
        cause,
      })
    }
    // 呼叫端主動取消：照原樣往上拋，那不是錯誤，呼叫端也不該把它顯示成失敗。
    if (callerSignal?.aborted) {
      throw cause
    }
    throw new ApiError({
      status: 0,
      code: CLIENT_ERROR_CODES.network,
      title: '無法連線',
      detail: '無法連線到伺服器，請檢查網路後重試',
      requestId: null,
      cause,
    })
  } finally {
    clearTimeout(timer)
  }

  if (!response.ok) {
    throw await toApiError(response)
  }

  // 204 與空 body 都要回 null：JSON.parse('') 會丟 SyntaxError，而那不是 ApiError，
  // 呼叫端的 catch 分支接不住，會變成未處理的例外。
  if (response.status === 204 || response.headers.get('Content-Length') === '0') {
    return null as T
  }
  const text = await response.text()
  return (text ? (JSON.parse(text) as T) : null) as T
}

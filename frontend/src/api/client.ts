/**
 * HTTP 傳輸層——前端唯一碰網路的地方（03 §2）。
 *
 * 職責：接 baseURL、逾時、把後端的 Problem Details（09 §1.3）正規化成一種例外、
 * **永遠不吞錯**，以及 401 refresh（03 §3.3，1E-1 進來）。SSE 走 `api/sse.ts`
 * （1E-3），刻意不在這裡預先鋪。
 *
 * 「只有一種例外」是這一層的核心價值：呼叫端不需要分辨 TypeError（網路斷）、
 * AbortError（逾時）與 HTTP 錯誤——它們在畫面上都是「這次操作失敗了」，
 * 差別只在 `code`。
 *
 * 認證的接線用 `configureAuth()` 注入而不是 import store：反向的話傳輸層就
 * 依賴了狀態層，任何 store 重構都會扯動這裡（1E-1 驗收測試釘住的契約）。
 */

import type { ProblemDetail, TokenPairOut } from '@/types/models'

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

// ── 認證接線（1E-1；03 §3.3）─────────────────────────────────────────────

export interface AuthHooks {
  /** 目前的 access token；`null` = 未登入（此時**不送** Authorization header）。 */
  getToken: () => string | null
  /** refresh 成功——store 據此換上新 token。 */
  onTokenRefreshed: (token: string) => void
  /** refresh 也失敗——session 真的結束了，store 清空、guard 送回登入頁。 */
  onSessionExpired: () => void
}

let authHooks: AuthHooks | null = null

/** 由 auth store 在建立時呼叫；傳 `null` 解除（測試隔離用）。 */
export function configureAuth(hooks: AuthHooks | null): void {
  authHooks = hooks
  refreshInFlight = null
}

/**
 * single-flight：所有並發的 401 共用同一次 refresh。
 *
 * 這不只是省流量——refresh rotation（1A-3）之下，舊 refresh token 用第二次
 * 會被後端視為重放攻擊而撤銷整個 token 家族：並發重複 refresh 等於自己登出自己。
 */
let refreshInFlight: Promise<string> | null = null

function sharedRefresh(hooks: AuthHooks): Promise<string> {
  refreshInFlight ??= (async () => {
    try {
      // 走 performRequest 而非 request：refresh 自己 401 時不准再觸發 refresh。
      const pair = await performRequest<TokenPairOut>(
        '/api/v1/auth/refresh',
        { method: 'POST', credentials: 'include' },
        null,
      )
      hooks.onTokenRefreshed(pair.access_token)
      return pair.access_token
    } catch (cause) {
      // 放在這裡而不是各個等待者的 catch：一次失敗只通知一次，
      // 不因並發等待者的數量而重複清空 store。
      hooks.onSessionExpired()
      throw cause
    } finally {
      refreshInFlight = null
    }
  })()
  return refreshInFlight
}

/** 這些路徑的 401 有各自的語意（帳密錯、無 session），refresh 幫不上忙。 */
const isAuthPath = (path: string): boolean => path.startsWith('/api/v1/auth/')

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
 * 401 且 `code === 'AUTH_TOKEN_EXPIRED'`（09 附錄 A：該碼的語意就是「client 走
 * refresh」）時 refresh 後帶新 token 重放**一次**。上限一次是硬的：後端若因
 * 時鐘偏移把新 token 也判過期，沒有上限就是無窮迴圈。其他 401（帳密錯、token
 * 被撤銷）直接放行——對它們 refresh 只是把真正的錯誤遮起來。
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const hooks = authHooks
  const usedToken = hooks?.getToken() ?? null

  try {
    return await performRequest<T>(path, options, usedToken)
  } catch (error) {
    if (
      hooks === null ||
      !(error instanceof ApiError) ||
      error.status !== 401 ||
      error.code !== 'AUTH_TOKEN_EXPIRED' ||
      isAuthPath(path)
    ) {
      throw error
    }

    // 另一個請求可能已經 refresh 完了（我們失敗的原因是帶著舊 token 出門）——
    // 那就直接帶現在的 token 重放，不再多打一次 refresh。
    const current = hooks.getToken()
    if (current !== null && current !== usedToken) {
      return performRequest<T>(path, options, current)
    }

    let fresh: string
    try {
      fresh = await sharedRefresh(hooks)
    } catch {
      // refresh 的失敗細節不往上冒：呼叫端在等的是「原本那個請求」的結果，
      // 拿到 refresh 端點的錯誤只會誤導。session 清理已由 onSessionExpired 做掉。
      throw error
    }
    return performRequest<T>(path, options, fresh)
  }
}

/**
 * 實際發出一個請求（不含 refresh 邏輯）。
 *
 * 逾時用 AbortController 而非只是 reject：只 reject 的話連線會留著直到伺服器
 * 回應，使用者反覆切分頁時會累積。
 */
async function performRequest<T>(
  path: string,
  options: RequestOptions,
  token: string | null,
): Promise<T> {
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
      headers: {
        Accept: 'application/json',
        // 沒 token 就**不送** Authorization：送 `Bearer null` 這種字串，後端
        // 回 401 且 log 裡看起來像攻擊嘗試。
        ...(token !== null ? { Authorization: `Bearer ${token}` } : {}),
        ...headers,
      },
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

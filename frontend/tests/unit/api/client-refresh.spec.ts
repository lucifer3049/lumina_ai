/**
 * 驗收：client.ts 的 401 refresh（1E-1；03 §3.3、09 §2.1、附錄 A）。
 *
 * 03 §3.3：「401 時 client.ts 統一 refresh 後重放原請求（single-flight，避免並發
 * 重複 refresh）」——這一層住在 client 而不是 store，因為每一個 API 呼叫點都需要
 * 它，而 store 只該被「登入頁」與「登出鈕」直接碰到。
 *
 * client 與 store 的接線用 `configureAuth({ getToken, onTokenRefreshed,
 * onSessionExpired })` 注入，不讓 client import store——反向的話 api/ 層就依賴了
 * 狀態層，之後任何 store 重構都會扯動傳輸層。
 *
 * 只對 code === 'AUTH_TOKEN_EXPIRED' 走 refresh（09 附錄 A 明訂該碼的語意就是
 * 「client 走 refresh」）。其他 401（密碼錯、token 被撤銷）直接放行——對它們
 * refresh 是在遮掩真正的錯誤，且被撤銷的 session 會因此多打一次必敗的請求。
 */
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, configureAuth, request } from '@/api/client'

const BASE_URL = 'http://api.test'
const server = setupServer()

/** 後端過期 token 的標準回應（api/main.py 的 problem_response）。 */
const expiredProblem = () =>
  HttpResponse.json(
    {
      type: '/errors/auth-token-expired',
      title: 'Token expired',
      status: 401,
      code: 'AUTH_TOKEN_EXPIRED',
    },
    { status: 401, headers: { 'Content-Type': 'application/problem+json' } },
  )

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  // 每條測試自己接線；殘留的 callbacks 會讓測試間互相汙染。
  configureAuth(null)
})
afterAll(() => server.close())

describe('Authorization header 注入', () => {
  it('attaches Bearer token from getToken on every request', async () => {
    let seen: string | null = null
    configureAuth({
      getToken: () => 'tok-1',
      onTokenRefreshed: () => {},
      onSessionExpired: () => {},
    })
    server.use(
      http.get(`${BASE_URL}/api/v1/things`, ({ request: incoming }) => {
        seen = incoming.headers.get('Authorization')
        return HttpResponse.json({})
      }),
    )

    await request('/api/v1/things')

    expect(seen).toBe('Bearer tok-1')
  })

  it('sends no Authorization header when there is no token', async () => {
    // 未登入時送 `Authorization: Bearer null` 這種字串，後端會回 401 而且
    // log 裡看起來像攻擊嘗試。沒有就是不送。
    let seen: string | null = 'sentinel'
    configureAuth({
      getToken: () => null,
      onTokenRefreshed: () => {},
      onSessionExpired: () => {},
    })
    server.use(
      http.get(`${BASE_URL}/api/v1/public`, ({ request: incoming }) => {
        seen = incoming.headers.get('Authorization')
        return HttpResponse.json({})
      }),
    )

    await request('/api/v1/public')

    expect(seen).toBeNull()
  })
})

describe('401 AUTH_TOKEN_EXPIRED → refresh → 重放（03 §3.3）', () => {
  let token: string
  let refreshCalls: number

  beforeEach(() => {
    token = 'stale'
    refreshCalls = 0
    configureAuth({
      getToken: () => token,
      onTokenRefreshed: (t) => {
        token = t
      },
      onSessionExpired: () => {},
    })
    server.use(
      http.post(`${BASE_URL}/api/v1/auth/refresh`, () => {
        refreshCalls += 1
        return HttpResponse.json({ access_token: 'fresh', token_type: 'Bearer', expires_in: 900 })
      }),
    )
  })

  it('replays the original request with the new token and resolves', async () => {
    const authHeaders: (string | null)[] = []
    server.use(
      http.get(`${BASE_URL}/api/v1/things`, ({ request: incoming }) => {
        authHeaders.push(incoming.headers.get('Authorization'))
        return authHeaders.length === 1 ? expiredProblem() : HttpResponse.json({ ok: true })
      }),
    )

    await expect(request<{ ok: boolean }>('/api/v1/things')).resolves.toEqual({ ok: true })
    // 重放必須帶**新** token——帶舊的會再吃一次 401，看起來像 refresh 沒生效。
    expect(authHeaders).toEqual(['Bearer stale', 'Bearer fresh'])
    expect(refreshCalls).toBe(1)
  })

  it('single-flight: concurrent 401s share one refresh call', async () => {
    // refresh rotation 之下這不只是省流量：舊 refresh token 用第二次會被後端
    // 視為重放攻擊而撤銷整個家族（1A-3），並發重複 refresh 等於自己登出自己。
    server.use(
      http.get(`${BASE_URL}/api/v1/a`, ({ request: incoming }) =>
        incoming.headers.get('Authorization') === 'Bearer fresh'
          ? HttpResponse.json({ from: 'a' })
          : expiredProblem(),
      ),
      http.get(`${BASE_URL}/api/v1/b`, ({ request: incoming }) =>
        incoming.headers.get('Authorization') === 'Bearer fresh'
          ? HttpResponse.json({ from: 'b' })
          : expiredProblem(),
      ),
    )

    const [a, b] = await Promise.all([
      request<{ from: string }>('/api/v1/a'),
      request<{ from: string }>('/api/v1/b'),
    ])

    expect([a.from, b.from]).toEqual(['a', 'b'])
    expect(refreshCalls).toBe(1)
  })

  it('retries at most once — a second 401 propagates, no refresh loop', async () => {
    // 後端若因時鐘偏移把新 token 也判過期，沒有這條上限就是無窮迴圈：
    // 畫面卡住、網路面板裡 refresh 刷屏。
    server.use(http.get(`${BASE_URL}/api/v1/things`, () => expiredProblem()))

    const error = (await request('/api/v1/things').catch((e: unknown) => e)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(401)
    expect(refreshCalls).toBe(1)
  })
})

describe('refresh 失敗與豁免路徑', () => {
  it('fires onSessionExpired once and rejects when refresh itself 401s', async () => {
    const onSessionExpired = vi.fn()
    configureAuth({ getToken: () => 'stale', onTokenRefreshed: () => {}, onSessionExpired })
    server.use(
      http.get(`${BASE_URL}/api/v1/things`, () => expiredProblem()),
      http.post(`${BASE_URL}/api/v1/auth/refresh`, () => expiredProblem()),
    )

    const error = (await request('/api/v1/things').catch((e: unknown) => e)) as ApiError

    // session 真的結束了：呼叫端拿到原始 401，store 經 onSessionExpired 清空、
    // guard 把人送回登入頁。兩件事缺一不可。
    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(401)
    expect(onSessionExpired).toHaveBeenCalledTimes(1)
  })

  it('does not attempt refresh for non-expired 401 codes', async () => {
    // 密碼錯（AUTH_INVALID_CREDENTIALS）也是 401；對它 refresh 只會把「帳密錯了」
    // 變成「不明原因失敗」。
    let refreshCalls = 0
    configureAuth({ getToken: () => null, onTokenRefreshed: () => {}, onSessionExpired: () => {} })
    server.use(
      http.post(`${BASE_URL}/api/v1/auth/login`, () =>
        HttpResponse.json(
          { title: 'Invalid credentials', status: 401, code: 'AUTH_INVALID_CREDENTIALS' },
          { status: 401, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
      http.post(`${BASE_URL}/api/v1/auth/refresh`, () => {
        refreshCalls += 1
        return HttpResponse.json({ access_token: 'x', token_type: 'Bearer', expires_in: 900 })
      }),
    )

    const error = (await request('/api/v1/auth/login', { method: 'POST' }).catch(
      (e: unknown) => e,
    )) as ApiError

    expect(error.code).toBe('AUTH_INVALID_CREDENTIALS')
    expect(refreshCalls).toBe(0)
  })
})

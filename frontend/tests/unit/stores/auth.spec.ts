/**
 * 驗收：stores/auth.ts（1E-1；03 §2、§3.3、09 §2.1）。
 *
 * 存放策略是這個 store 的核心價值：access token 只活在 Pinia 記憶體，**永遠
 * 不落地**（03 §3.3——localStorage 是 XSS 的直接目標）；refresh token 由後端放在
 * httpOnly cookie，JavaScript 根本讀不到。代價是重新整理會失去 access token，
 * 所以 `bootstrap()` 在 app 啟動時拿 cookie 換一顆新的——「重新整理不掉登入」
 * 是靠它成立的，本檔的測試直接盯著這件事。
 *
 * 測試跑在 node 環境（vite.config.ts 的 test.environment），這裡**沒有**
 * localStorage——store 若嘗試落地，測試會直接炸，這是刻意的守門。
 */
import { createPinia, setActivePinia } from 'pinia'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'

import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const BASE_URL = 'http://api.test'
const server = setupServer()

const ME = {
  id: '3f9f2b1e-0000-4000-8000-000000000001',
  email: 'hoa@acme.test',
  display_name: 'Hoa',
  status: 'active',
  roles: ['tenant_admin'],
}

/** 登入成功的兩個 handler：POST /auth/login 與緊接著的 GET /users/me。 */
const loginHandlers = (accessToken = 'tok-login') => [
  http.post(`${BASE_URL}/api/v1/auth/login`, () =>
    HttpResponse.json({ access_token: accessToken, token_type: 'Bearer', expires_in: 900 }),
  ),
  http.get(`${BASE_URL}/api/v1/users/me`, ({ request: incoming }) =>
    incoming.headers.get('Authorization') === `Bearer ${accessToken}`
      ? HttpResponse.json(ME)
      : HttpResponse.json(
          { title: 'Unauthorized', status: 401, code: 'AUTH_TOKEN_EXPIRED' },
          { status: 401, headers: { 'Content-Type': 'application/problem+json' } },
        ),
  ),
]

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
beforeEach(() => setActivePinia(createPinia()))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

describe('login', () => {
  it('sends tenant_slug + email + password and becomes authenticated', async () => {
    // tenant_slug 是必填的：登入發生在租戶身分存在之前，RLS 之下沒有租戶
    // 就查不到任何使用者（backend/api/schemas/auth.py 的 LoginIn）。
    let body: unknown
    server.use(
      http.post(`${BASE_URL}/api/v1/auth/login`, async ({ request: incoming }) => {
        body = await incoming.json()
        return HttpResponse.json({ access_token: 'tok-login', token_type: 'Bearer', expires_in: 900 })
      }),
      ...loginHandlers().slice(1),
    )
    const store = useAuthStore()

    await store.login({ tenantSlug: 'acme', email: 'hoa@acme.test', password: 'pw' })

    expect(body).toEqual({ tenant_slug: 'acme', email: 'hoa@acme.test', password: 'pw' })
    expect(store.isAuthenticated).toBe(true)
    // 登入後立刻取 /users/me——名字與角色是 DefaultLayout 和 v-permission 的
    // 資料來源，沒有它畫面上只有一顆 token。
    expect(store.user).toMatchObject({ email: 'hoa@acme.test', roles: ['tenant_admin'] })
  })

  it('surfaces ApiError and stays logged out on wrong credentials', async () => {
    server.use(
      http.post(`${BASE_URL}/api/v1/auth/login`, () =>
        HttpResponse.json(
          { title: 'Invalid credentials', status: 401, code: 'AUTH_INVALID_CREDENTIALS' },
          { status: 401, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const store = useAuthStore()

    const error = (await store
      .login({ tenantSlug: 'acme', email: 'hoa@acme.test', password: 'nope' })
      .catch((e: unknown) => e)) as ApiError

    // 錯誤要原樣冒出去：LoginView 靠 code 決定顯示什麼訊息，store 吞掉的話
    // 畫面只能顯示「登入失敗」而說不出為什麼。
    expect(error).toBeInstanceOf(ApiError)
    expect(error.code).toBe('AUTH_INVALID_CREDENTIALS')
    expect(store.isAuthenticated).toBe(false)
    expect(store.user).toBeNull()
  })
})

describe('bootstrap（重新整理後的 session 還原）', () => {
  it('restores the session from the refresh cookie', async () => {
    server.use(
      http.post(`${BASE_URL}/api/v1/auth/refresh`, () =>
        HttpResponse.json({ access_token: 'tok-boot', token_type: 'Bearer', expires_in: 900 }),
      ),
      http.get(`${BASE_URL}/api/v1/users/me`, () => HttpResponse.json(ME)),
    )
    const store = useAuthStore()

    await store.bootstrap()

    expect(store.isAuthenticated).toBe(true)
    expect(store.user).toMatchObject({ email: 'hoa@acme.test' })
  })

  it('resolves without throwing when there is no session', async () => {
    // 匿名訪客開站是每天發生的正常路徑，不是錯誤——bootstrap 丟例外的話
    // app 啟動就掛在一個「沒登入」上。
    server.use(
      http.post(`${BASE_URL}/api/v1/auth/refresh`, () =>
        HttpResponse.json(
          { title: 'Unauthorized', status: 401, code: 'AUTH_TOKEN_INVALID' },
          { status: 401, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const store = useAuthStore()

    await expect(store.bootstrap()).resolves.toBeUndefined()

    expect(store.isAuthenticated).toBe(false)
  })

  it('runs at most once — a second call does not hit the network again', async () => {
    // guard 在**每次**導航都會確保 bootstrap 完成；不設一次上限的話，每換一頁
    // 就多一次 refresh，而 rotation 之下那還會撤銷舊 token 家族。
    let refreshCalls = 0
    server.use(
      http.post(`${BASE_URL}/api/v1/auth/refresh`, () => {
        refreshCalls += 1
        return HttpResponse.json(
          { title: 'Unauthorized', status: 401, code: 'AUTH_TOKEN_INVALID' },
          { status: 401, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }),
    )
    const store = useAuthStore()

    await store.bootstrap()
    await store.bootstrap()

    expect(refreshCalls).toBe(1)
  })
})

describe('logout', () => {
  it('revokes the session server-side and clears local state', async () => {
    let revoked = false
    server.use(
      ...loginHandlers(),
      http.post(`${BASE_URL}/api/v1/auth/logout`, () => {
        revoked = true
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const store = useAuthStore()
    await store.login({ tenantSlug: 'acme', email: 'hoa@acme.test', password: 'pw' })

    await store.logout()

    // 兩邊都要發生：只清本地，refresh cookie 還活著（下一個 bootstrap 又登回來）；
    // 只打後端，畫面還掛著上一個人的名字。
    expect(revoked).toBe(true)
    expect(store.isAuthenticated).toBe(false)
    expect(store.user).toBeNull()
  })

  it('clears local state even when the logout request fails', async () => {
    // 網路斷線時按登出——本地一定要清，不能把使用者困在一個登不出去的 session。
    // 伺服器端那份由 refresh token 的 7 天 TTL 兜底。
    server.use(...loginHandlers(), http.post(`${BASE_URL}/api/v1/auth/logout`, () => HttpResponse.error()))
    const store = useAuthStore()
    await store.login({ tenantSlug: 'acme', email: 'hoa@acme.test', password: 'pw' })

    await store.logout()

    expect(store.isAuthenticated).toBe(false)
    expect(store.user).toBeNull()
  })
})

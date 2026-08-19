/**
 * 驗收：router/guards.ts（1E-1；03 §2）。
 *
 * guard 的職責只有「沒登入就送去登入頁、登入了就別再看登入頁」——權限的最終
 * 裁決在後端（03 §1），前端 guard 是 UX，不是安全邊界。
 *
 * 測試用自建的 memory-history router 而不是 src/router/index.ts 的單例：guard 的
 * 行為與路由表是兩件事，綁著測的話每加一條真實路由都可能弄紅這裡。路由表
 * 本身的約定（/login 存在且 public、其餘預設要登入）由最後一組測試對真實
 * router 驗證。
 */
import { createPinia, setActivePinia } from 'pinia'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'

import { installGuards } from '@/router/guards'
import { useAuthStore } from '@/stores/auth'

const BASE_URL = 'http://api.test'
const server = setupServer()

/** 沒有 session 時的 refresh 回應——guard 首次導航會觸發 bootstrap。 */
const noSession = () =>
  http.post(`${BASE_URL}/api/v1/auth/refresh`, () =>
    HttpResponse.json(
      { title: 'Unauthorized', status: 401, code: 'AUTH_TOKEN_INVALID' },
      { status: 401, headers: { 'Content-Type': 'application/problem+json' } },
    ),
  )

const ME = {
  id: '3f9f2b1e-0000-4000-8000-000000000001',
  email: 'hoa@acme.test',
  display_name: 'Hoa',
  status: 'active',
  roles: ['tenant_admin'],
}

/** 與真實路由表同形狀的最小路由：一條 public（login）、兩條受保護頁。 */
function buildRouter(): Router {
  const Noop = { render: () => null }
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'home', component: Noop },
      { path: '/login', name: 'login', component: Noop, meta: { public: true } },
      { path: '/knowledge', name: 'knowledge', component: Noop },
    ],
  })
  installGuards(router)
  return router
}

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
beforeEach(() => setActivePinia(createPinia()))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

describe('未登入', () => {
  it('redirects protected routes to /login and remembers the target', async () => {
    server.use(noSession())
    const router = buildRouter()

    await router.push('/knowledge')

    expect(router.currentRoute.value.name).toBe('login')
    // redirect query 是「登入後回到原本要去的頁」的唯一線索；丟掉它，
    // 使用者貼給同事的深層連結每次都落在首頁。
    expect(router.currentRoute.value.query.redirect).toBe('/knowledge')
  })

  it('leaves public routes reachable', async () => {
    server.use(noSession())
    const router = buildRouter()

    await router.push('/login')

    expect(router.currentRoute.value.name).toBe('login')
  })
})

describe('已登入', () => {
  async function loggedInRouter(): Promise<Router> {
    server.use(
      http.post(`${BASE_URL}/api/v1/auth/login`, () =>
        HttpResponse.json({ access_token: 'tok', token_type: 'Bearer', expires_in: 900 }),
      ),
      http.get(`${BASE_URL}/api/v1/users/me`, () => HttpResponse.json(ME)),
    )
    await useAuthStore().login({ tenantSlug: 'acme', email: 'hoa@acme.test', password: 'pw' })
    return buildRouter()
  }

  it('lets authenticated users into protected routes', async () => {
    const router = await loggedInRouter()

    await router.push('/knowledge')

    expect(router.currentRoute.value.name).toBe('knowledge')
  })

  it('bounces authenticated users away from /login', async () => {
    // 已登入還停在登入頁是一個死路：表單送出會因 session 已存在而語意不明。
    const router = await loggedInRouter()

    await router.push('/login')

    expect(router.currentRoute.value.name).toBe('home')
  })
})

describe('session 還原（重新整理的等價情境）', () => {
  it('restores the session via bootstrap before deciding, exactly once', async () => {
    // 重新整理後 access token 沒了但 refresh cookie 還在——guard 必須先等
    // bootstrap 完成再裁決，否則每次 F5 都被踢回登入頁。
    let refreshCalls = 0
    server.use(
      http.post(`${BASE_URL}/api/v1/auth/refresh`, () => {
        refreshCalls += 1
        return HttpResponse.json({ access_token: 'tok-boot', token_type: 'Bearer', expires_in: 900 })
      }),
      http.get(`${BASE_URL}/api/v1/users/me`, () => HttpResponse.json(ME)),
    )
    const router = buildRouter()

    await router.push('/knowledge')
    await router.push('/')

    expect(router.currentRoute.value.name).toBe('home')
    // 第二次導航不准再打 refresh（bootstrap 一次上限，stores/auth.spec.ts 同款）。
    expect(refreshCalls).toBe(1)
  })
})

describe('真實路由表的約定（src/router/index.ts）', () => {
  it('has a public /login and keeps every other route protected', async () => {
    const { default: appRouter } = await import('@/router')

    const login = appRouter.getRoutes().find((r) => r.name === 'login')
    expect(login, '路由表缺 /login').toBeDefined()
    expect(login?.meta.public).toBe(true)

    // 「受保護」是**預設**而不是逐條加註：新頁面忘了標 meta 時，錯的方向
    // 必須是「多擋」而不是「漏擋」。
    const unprotected = appRouter
      .getRoutes()
      .filter((r) => r.meta.public === true && r.name !== 'login')
    expect(unprotected, '除 /login 外不應有 public 路由').toEqual([])
  })
})

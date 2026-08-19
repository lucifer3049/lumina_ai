/**
 * 認證狀態（1E-1；03 §2、§3.3）。
 *
 * 存放策略是這個 store 的核心價值：access token 只活在記憶體，**永遠不落地**
 * ——localStorage 是 XSS 的直接目標（03 §3.3），而 refresh token 由後端放在
 * httpOnly cookie，JavaScript 根本讀不到。代價是重新整理會失去 access token，
 * 由 `bootstrap()` 在首次導航時拿 cookie 換一顆新的補回來。
 *
 * 與傳輸層的接線走 `configureAuth()` 注入（client.ts 不 import 這裡），
 * 在 store 建立時完成——pinia 每次 `createPinia()` 都是新的 store 實例，
 * 測試因此天然隔離。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { configureAuth, request } from '@/api/client'
import type { TokenPairOut, UserOut } from '@/types/models'

export interface LoginInput {
  /** 登入發生在租戶身分存在之前（RLS 之下沒租戶查不到人），所以必填。 */
  tenantSlug: string
  email: string
  password: string
}

export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref<string | null>(null)
  const user = ref<UserOut | null>(null)
  /**
   * bootstrap 的一次上限：guard 每次導航都會 await 它，memoize 讓第二次起
   * 都是免費的。不用 ref——它是控制流程的細節，不是要給畫面看的狀態。
   */
  let bootstrapPromise: Promise<void> | null = null

  configureAuth({
    getToken: () => accessToken.value,
    onTokenRefreshed: (token) => {
      accessToken.value = token
    },
    onSessionExpired: () => {
      accessToken.value = null
      user.value = null
    },
  })

  const isAuthenticated = computed(() => accessToken.value !== null)

  /** 登入成功後立刻取 `/users/me`：名字與角色是 layout 與 v-permission 的資料來源。 */
  async function login(input: LoginInput): Promise<void> {
    const pair = await request<TokenPairOut>('/api/v1/auth/login', {
      method: 'POST',
      credentials: 'include', // 後端在這個回應上種 refresh cookie
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_slug: input.tenantSlug,
        email: input.email,
        password: input.password,
      }),
    })
    accessToken.value = pair.access_token
    user.value = await request<UserOut>('/api/v1/users/me')
  }

  /**
   * 重新整理後的 session 還原：refresh cookie 還在的話換一顆新 access token。
   *
   * 失敗**不拋**——匿名訪客開站是每天發生的正常路徑，不是錯誤；app 啟動
   * 不能掛在一個「沒登入」上。
   */
  function bootstrap(): Promise<void> {
    bootstrapPromise ??= (async () => {
      if (accessToken.value !== null) {
        return // 已經登入（同一個 SPA session 內先 login 後導航）
      }
      try {
        const pair = await request<TokenPairOut>('/api/v1/auth/refresh', {
          method: 'POST',
          credentials: 'include',
        })
        accessToken.value = pair.access_token
        user.value = await request<UserOut>('/api/v1/users/me')
      } catch {
        // 401 = 沒有 session；網路錯也一樣當未登入處理——之後的任何 API
        // 呼叫都會把真正的錯誤帶到畫面上，這裡不需要搶著報。
      }
    })()
    return bootstrapPromise
  }

  /**
   * 登出兩邊都要發生：只清本地，refresh cookie 還活著（下一次 bootstrap 又
   * 登回來）；只打後端，畫面還掛著上一個人的名字。後端打不到（斷網）時本地
   * 照清——不能把使用者困在登不出去的 session，伺服器端由 refresh TTL 兜底。
   */
  async function logout(): Promise<void> {
    try {
      await request<null>('/api/v1/auth/logout', { method: 'POST', credentials: 'include' })
    } catch {
      // 見 docstring：本地清理在 finally，這裡沒有要補救的事。
    } finally {
      accessToken.value = null
      user.value = null
    }
  }

  return { accessToken, user, isAuthenticated, login, bootstrap, logout }
})

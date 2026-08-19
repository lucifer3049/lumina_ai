/**
 * Route 定義（03 §2）。
 *
 * view 一律 lazy import：路由級 code splitting 是預設行為，等到頁面變多才補會
 * 需要回頭改每一條 route。
 *
 * 「受保護」是預設（guards.ts）：只有顯式標 `meta: { public: true }` 的路由
 * 不需要登入——目前只有 /login，且驗收測試盯著「除它之外不得有 public 路由」。
 */
import { createMemoryHistory, createRouter, createWebHistory } from 'vue-router'

import { installGuards } from './guards'

const router = createRouter({
  // 非瀏覽器環境（vitest 的 node 環境要驗真實路由表）沒有 window，
  // 按 vue-router 官方做法退到 memory history。
  history:
    typeof window === 'undefined'
      ? createMemoryHistory(import.meta.env.BASE_URL)
      : createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'home',
      component: () => import('@/views/HomeView.vue'),
    },
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/auth/LoginView.vue'),
      meta: { public: true },
    },
  ],
})

installGuards(router)

export default router

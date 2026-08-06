/**
 * Route 定義（03 §2）。
 *
 * view 一律 lazy import：路由級 code splitting 是預設行為，等到頁面變多才補會
 * 需要回頭改每一條 route。guards.ts（auth / permission / tenant）屬 1A，尚未建立。
 */
import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'home',
      component: () => import('@/views/HomeView.vue'),
    },
  ],
})

export default router

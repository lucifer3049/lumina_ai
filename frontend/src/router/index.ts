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
      path: '/knowledge',
      name: 'knowledge',
      component: () => import('@/views/knowledge/KnowledgeBaseListView.vue'),
    },
    {
      // props: true —— view 收 kbId 當 prop 而不是自己讀 useRoute()：
      // 參數變了（同一頁換 KB）時 prop 會觸發 watch，讀 route 的寫法要另外接線。
      path: '/knowledge/:kbId',
      name: 'knowledge-documents',
      component: () => import('@/views/knowledge/DocumentListView.vue'),
      props: true,
    },
    {
      // 兩條 chat 路由共用同一個 view（ChatGPT 版式：側欄常駐，`/chat` 是「新對話」
      // 狀態）。`meta.bare`＝內容區不加 padding，版面（貼緣側欄）由頁面自己管。
      path: '/chat',
      name: 'chat',
      component: () => import('@/views/chat/ConversationView.vue'),
      meta: { bare: true },
    },
    {
      path: '/chat/:conversationId',
      name: 'chat-conversation',
      component: () => import('@/views/chat/ConversationView.vue'),
      props: true,
      meta: { bare: true },
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

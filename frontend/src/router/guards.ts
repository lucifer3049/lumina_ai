/**
 * 導航守衛（1E-1；03 §2）。
 *
 * 職責只有「沒登入就送去登入頁、登入了就別再看登入頁」。權限的最終裁決在
 * 後端（03 §1）——這裡是 UX，不是安全邊界；繞過 guard 的人拿到的每個 API
 * 都還是 401/403。
 *
 * 「受保護」是**預設**：路由要開放得顯式標 `meta: { public: true }`。新頁面
 * 忘了標的時候，錯的方向是「多擋」（登入的人照樣看得到），而不是「漏擋」。
 */
import type { Router } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

export function installGuards(router: Router): void {
  router.beforeEach(async (to) => {
    const auth = useAuthStore()

    // 先等 session 還原再裁決，否則每次 F5 都被踢回登入頁。bootstrap 內建
    // 一次上限（memoized promise），第二次導航起這行是免費的。
    await auth.bootstrap()

    if (to.meta.public === true) {
      // 已登入還停在登入頁是死路：表單送出的語意（已有 session）不明。
      if (auth.isAuthenticated && to.name === 'login') {
        return { name: 'home' }
      }
      return true
    }

    if (!auth.isAuthenticated) {
      // redirect query 是「登入後回到原本要去的頁」的唯一線索——深層連結
      // 靠它才不會每次都落在首頁。
      return { name: 'login', query: { redirect: to.fullPath } }
    }
    return true
  })
}

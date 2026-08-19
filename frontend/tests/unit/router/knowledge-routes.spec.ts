/**
 * 驗收：1E-2 的路由（03 §2）。
 *
 * guards.spec.ts 驗的是 guard 的行為與「除 /login 外零 public」的通則；這裡驗的是
 * 1E-2 真的把兩個頁面接上了路由表，且維持三個約定：lazy import（路由級分包）、
 * 受保護（不標 public）、以及 KB 詳情頁用 `kbId` 這個參數名——側邊選單、麵包屑
 * 與 view 的 props 都靠它，改名時會有三個地方安靜地拿到 undefined。
 */
import { describe, expect, it } from 'vitest'

describe('知識庫路由', () => {
  it('registers the list and the document pages', async () => {
    const { default: router } = await import('@/router')
    const names = router.getRoutes().map((route) => route.name)

    expect(names).toContain('knowledge')
    expect(names).toContain('knowledge-documents')
  })

  it('keeps both protected and lazily loaded', async () => {
    const { default: router } = await import('@/router')

    for (const name of ['knowledge', 'knowledge-documents']) {
      const route = router.getRoutes().find((r) => r.name === name)
      expect(route, `路由表缺 ${name}`).toBeDefined()
      expect(route?.meta.public, `${name} 不該是 public`).toBeUndefined()
      // component 是函式 = 還沒被載進來（lazy）。直接 import 的話首屏會把
      // 知識庫頁一起拖下來，而登入頁的人根本用不到它。
      expect(typeof route?.components?.default, `${name} 沒有 lazy import`).toBe('function')
    }
  })

  it('passes the KB id through as the kbId param', async () => {
    const { default: router } = await import('@/router')

    const resolved = router.resolve('/knowledge/3f9f2b1e-0000-4000-8000-00000000000a')

    expect(resolved.name).toBe('knowledge-documents')
    expect(resolved.params.kbId).toBe('3f9f2b1e-0000-4000-8000-00000000000a')
  })
})

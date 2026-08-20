/**
 * 驗收：1E-3 的路由（03 §2）。
 *
 * 對話有兩個入口：清單（`/chat`）與單一對話（`/chat/:conversationId`）。後者的參數
 * 名是 view 的 prop 來源，改名時 view 會安靜地拿到 undefined，然後對
 * `/conversations/undefined/messages` 發請求——404 的錯誤訊息完全不會提到路由。
 */
import { describe, expect, it } from 'vitest'

describe('對話路由', () => {
  it('registers the list and the conversation pages', async () => {
    const { default: router } = await import('@/router')
    const names = router.getRoutes().map((route) => route.name)

    expect(names).toContain('chat')
    expect(names).toContain('chat-conversation')
  })

  it('keeps both protected and lazily loaded', async () => {
    const { default: router } = await import('@/router')

    for (const name of ['chat', 'chat-conversation']) {
      const route = router.getRoutes().find((r) => r.name === name)
      expect(route, `路由表缺 ${name}`).toBeDefined()
      expect(route?.meta.public, `${name} 不該是 public`).toBeUndefined()
      expect(typeof route?.components?.default, `${name} 沒有 lazy import`).toBe('function')
    }
  })

  it('passes the conversation id through as conversationId', async () => {
    const { default: router } = await import('@/router')

    const resolved = router.resolve('/chat/3f9f2b1e-0000-4000-8000-0000000000c1')

    expect(resolved.name).toBe('chat-conversation')
    expect(resolved.params.conversationId).toBe('3f9f2b1e-0000-4000-8000-0000000000c1')
  })
})

/**
 * 驗收：stores/chat.ts（1E-3；03 §2、09 §2.4、§3.2）。
 *
 * 這個 store 持有兩種東西，而它們的生命週期完全不同：
 *
 * - **已經定案的訊息**（`messages`）：後端是唯一真相。
 * - **正在生成的那一則**（`streaming`）：只活在記憶體裡，由 SSE 事件一段一段長出來。
 *
 * 兩者的交接點是 `done`——03 §3.2 訂的規則是「以 server 最終 message 覆蓋本地 buffer」。
 * 先清 buffer 再抓的話，畫面會空一下（回答消失又出現）；不覆蓋而直接把 buffer 當成
 * 定案訊息的話，`usage`、`citations` 與後端實際存的內容可能有出入（1D-4a 的不變式
 * 是「串流看到的 = 存下來的」，而**驗證它的方式**就是以存下來的為準）。
 */
import { createPinia, setActivePinia } from 'pinia'
import { HttpResponse, http, delay } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'

import { ApiError } from '@/api/client'
import { useChatStore } from '@/stores/chat'

const BASE_URL = 'http://api.test'
const C1 = '3f9f2b1e-0000-4000-8000-0000000000c1'
const C2 = '3f9f2b1e-0000-4000-8000-0000000000c2'
const M1 = 'm-assistant'
const M2 = 'm-assistant-2'
const server = setupServer()

const conversation = (id: string, title: string) => ({
  id,
  title,
  kb_ids: [],
  prompt_key: 'chat.default',
  status: 'active',
  pinned: false,
  message_count: 0,
  last_message_at: null,
})

const message = (id: string, role: string, content: string) => ({
  id,
  role,
  content,
  citations: [],
  model: 'mock',
  status: 'complete',
  usage: {},
  created_at: '2026-08-20T00:00:00Z',
})

const turn = {
  message_id: 'm-assistant',
  user_message_id: 'm-user',
  conversation_id: C1,
  stream_url: `/api/v1/conversations/${C1}/messages/m-assistant/stream`,
}

const problem = (status: number, code: string) =>
  HttpResponse.json(
    { type: 'about:blank', title: code, status, detail: '失敗', code, request_id: 'req-1' },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
beforeEach(() => setActivePinia(createPinia()))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

describe('對話清單', () => {
  it('loads conversations and clears loading even on failure', async () => {
    server.use(
      http.get(`${BASE_URL}/api/v1/conversations`, () =>
        HttpResponse.json({ items: [conversation(C1, '年假問題')], next_cursor: null }),
      ),
    )
    const store = useChatStore()

    await store.fetchConversations()
    expect(store.conversations.map((c) => c.title)).toEqual(['年假問題'])
    expect(store.loadingConversations).toBe(false)

    server.use(http.get(`${BASE_URL}/api/v1/conversations`, () => problem(500, 'INTERNAL_ERROR')))
    await expect(store.fetchConversations()).rejects.toBeInstanceOf(ApiError)
    expect(store.loadingConversations).toBe(false)
  })

  it('puts a new conversation at the top', async () => {
    // 後端列表是最近的在前（09 §2.4 的擁有者制清單），加在最後的話一重新整理就跳位。
    server.use(
      http.post(`${BASE_URL}/api/v1/conversations`, () =>
        HttpResponse.json(conversation(C2, '新對話'), { status: 201 }),
      ),
    )
    const store = useChatStore()
    store.conversations = [conversation(C1, '舊對話')]

    const created = await store.createConversation({ kb_ids: [] })

    expect(created.id).toBe(C2)
    expect(store.conversations.map((c) => c.id)).toEqual([C2, C1])
  })

  it('replaces a renamed conversation in place and drops a deleted one', async () => {
    server.use(
      http.patch(`${BASE_URL}/api/v1/conversations/${C1}`, () =>
        HttpResponse.json({ ...conversation(C1, '改過的標題') }),
      ),
      http.delete(
        `${BASE_URL}/api/v1/conversations/${C2}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const store = useChatStore()
    store.conversations = [conversation(C1, '原標題'), conversation(C2, '另一個')]

    await store.updateConversation(C1, { title: '改過的標題' })
    expect(store.conversations.map((c) => c.title)).toEqual(['改過的標題', '另一個'])

    await store.deleteConversation(C2)
    expect(store.conversations.map((c) => c.id)).toEqual([C1])
  })

  it('clears the open conversation when it is the one deleted', async () => {
    // 留著訊息清單的話，畫面上還開著一個已經不存在的對話，而送出下一句會 404。
    server.use(
      http.delete(
        `${BASE_URL}/api/v1/conversations/${C1}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const store = useChatStore()
    store.conversations = [conversation(C1, '要刪的')]
    store.currentConversationId = C1
    store.messages = [message('m1', 'user', '嗨')]

    await store.deleteConversation(C1)

    expect(store.currentConversationId).toBeNull()
    expect(store.messages).toEqual([])
  })
})

describe('訊息清單', () => {
  it('loads messages in time order', async () => {
    // 後端明說是時間**正序**（09 §2.4 的 list_messages）——1D-5 用同一條路徑組 context，
    // 前端若自己倒轉，畫面與模型看到的順序會不一致。
    server.use(
      http.get(`${BASE_URL}/api/v1/conversations/${C1}/messages`, () =>
        HttpResponse.json({
          items: [message('m1', 'user', '年假幾天'), message('m2', 'assistant', '14 天')],
          next_cursor: null,
        }),
      ),
    )
    const store = useChatStore()

    await store.fetchMessages(C1)

    expect(store.currentConversationId).toBe(C1)
    expect(store.messages.map((m) => m.id)).toEqual(['m1', 'm2'])
  })

  it('ignores a slow response for a conversation the user already left', async () => {
    server.use(
      http.get(`${BASE_URL}/api/v1/conversations/${C1}/messages`, async () => {
        await delay(50)
        return HttpResponse.json({ items: [message('m1', 'user', '舊的')], next_cursor: null })
      }),
      http.get(`${BASE_URL}/api/v1/conversations/${C2}/messages`, () =>
        HttpResponse.json({ items: [message('m9', 'user', '新的')], next_cursor: null }),
      ),
    )
    const store = useChatStore()

    const slow = store.fetchMessages(C1)
    await store.fetchMessages(C2)
    await slow

    expect(store.currentConversationId).toBe(C2)
    expect(store.messages.map((m) => m.id)).toEqual(['m9'])
  })
})

describe('送出一個問題（09 §2.4 拆成兩步的第一步）', () => {
  it('posts the content and opens a streaming buffer for the answer', async () => {
    let body: unknown = null
    server.use(
      http.post(`${BASE_URL}/api/v1/conversations/${C1}/messages`, async ({ request }) => {
        body = await request.json()
        return HttpResponse.json(turn, { status: 201 })
      }),
    )
    const store = useChatStore()
    store.currentConversationId = C1

    const started = await store.sendMessage(C1, '年假幾天？')

    expect(body).toEqual({ content: '年假幾天？' })
    expect(started.message_id).toBe('m-assistant')
    // 使用者那句要立刻出現：等串流開始才顯示的話，送出後畫面有一段空白，
    // 而使用者會以為沒送出去。
    expect(store.messages.at(-1)).toMatchObject({
      id: 'm-user',
      role: 'user',
      content: '年假幾天？',
    })
    // 回答的位置也要先占住（狀態列／游標畫在這裡）。
    expect(store.streaming).toMatchObject({
      messageId: 'm-assistant',
      text: '',
      status: 'streaming',
    })
  })

  it('leaves nothing behind when the send itself fails', async () => {
    // 送不出去時仍留著一個空的 streaming buffer，畫面會永遠停在「生成中」。
    server.use(
      http.post(`${BASE_URL}/api/v1/conversations/${C1}/messages`, () =>
        problem(429, 'RATE_LIMITED'),
      ),
    )
    const store = useChatStore()

    await expect(store.sendMessage(C1, '嗨')).rejects.toBeInstanceOf(ApiError)

    expect(store.streaming).toBeNull()
    expect(store.messages).toEqual([])
  })

  it('does not append into another conversation after a slow POST（2026-08-30 深度審查）', async () => {
    // `messages` 是全域單份、跟著 currentConversationId 走。慢速 POST 的回應在
    // 使用者已切到 B 之後才到——無條件 append 會把 A 的使用者氣泡塞進 B 的訊息
    // 列表，沒有任何錯誤，直到下次重抓才消失。
    server.use(
      http.post(`${BASE_URL}/api/v1/conversations/${C1}/messages`, async () => {
        await delay(30) // A 的 POST 很慢
        return HttpResponse.json(turn, { status: 201 })
      }),
      http.get(`${BASE_URL}/api/v1/conversations/${C2}/messages`, () =>
        HttpResponse.json({ items: [message(M2, 'assistant', 'B 的內容')], next_cursor: null }),
      ),
    )
    const store = useChatStore()
    store.currentConversationId = C1

    const pending = store.sendMessage(C1, '給 A 的問題')
    await store.fetchMessages(C2) // 切到 B，先完成
    await pending // A 的 POST 這時才 resolve

    expect(store.messages.map((item) => item.content)).toEqual(['B 的內容'])
    // 生成照常在背景跑（buffer 指名對話，元件依 current 決定渲染與否）。
    expect(store.streaming?.conversationId).toBe(C1)
  })
})

describe('串流中的 buffer', () => {
  function streamingStore() {
    const store = useChatStore()
    store.currentConversationId = C1
    store.beginStreaming({ messageId: M1, conversationId: C1 })
    return store
  }

  it('accumulates deltas in arrival order', () => {
    const store = streamingStore()

    store.appendDelta(M1, '年假')
    store.appendDelta(M1, '是 14 天')

    expect(store.streaming?.text).toBe('年假是 14 天')
  })

  it('keeps citations and usage from their own events', () => {
    const store = streamingStore()

    store.applyCitations(M1, [{ marker: '1', doc_name: 'a.pdf' }])
    store.applyUsage(M1, { prompt_tokens: 10, completion_tokens: 20, cost: null })

    expect(store.streaming?.citations).toHaveLength(1)
    expect(store.streaming?.usage).toMatchObject({ completion_tokens: 20 })
  })

  it('replaces the buffer with the server message when the turn completes', async () => {
    // 03 §3.2：`done` 之後以 server 的最終 message 為準。順序是「先抓、後清」——
    // 反過來畫面會空一下，回答消失又出現。
    server.use(
      http.get(`${BASE_URL}/api/v1/conversations/${C1}/messages`, () =>
        HttpResponse.json({
          items: [
            message('m-user', 'user', '年假幾天'),
            {
              ...message('m-assistant', 'assistant', '年假是 14 天[c:1]'),
              citations: [{ marker: '1' }],
            },
          ],
          next_cursor: null,
        }),
      ),
    )
    const store = streamingStore()
    store.appendDelta(M1, '年假是 14 天')

    await store.finishStreaming(M1)

    expect(store.streaming).toBeNull()
    expect(store.messages.at(-1)).toMatchObject({ id: 'm-assistant', content: '年假是 14 天[c:1]' })
  })

  it('keeps the partial answer visible when the stream errors', () => {
    // 09 §3.2 的 error 事件：HTTP 早就 200 了，已經送出的字是有效內容。
    // 清掉它等於把使用者已經讀到的東西沒收。
    const store = streamingStore()
    store.appendDelta(M1, '已經講到一半')

    store.failStreaming(M1, { code: 'PROVIDER_UNAVAILABLE', title: '模型暫時不可用', retryable: true })

    expect(store.streaming?.text).toBe('已經講到一半')
    expect(store.streaming?.status).toBe('error')
    expect(store.streaming?.error).toMatchObject({ code: 'PROVIDER_UNAVAILABLE', retryable: true })
  })

  it('asks the backend to stop and marks the buffer stopped', async () => {
    // 停止是 202「已受理、還沒發生」（真正停下來的是另一個行程裡的 task）。
    let called = false
    server.use(
      http.post(`${BASE_URL}/api/v1/conversations/${C1}/messages/m-assistant/stop`, () => {
        called = true
        return HttpResponse.json({}, { status: 202 })
      }),
    )
    const store = streamingStore()
    store.appendDelta(M1, '講到一半被按停')

    await store.stopStreaming()

    expect(called).toBe(true)
    expect(store.streaming?.status).toBe('stopping')
    expect(store.streaming?.text).toBe('講到一半被按停')
  })

  it('ignores stray deltas that arrive with no active buffer', () => {
    // 換頁之後才到的事件。沒有這條守門的話，上一則回答的尾巴會長在新頁面上。
    const store = useChatStore()

    store.appendDelta(M1, '遲到的字')

    expect(store.streaming).toBeNull()
  })

  it('counts a pending stop as still generating', () => {
    // 202 是「已受理、還沒發生」：這時放開輸入框，使用者送下一句會蓋掉還在寫的 buffer。
    const store = streamingStore()
    expect(store.isGenerating).toBe(true)

    store.streaming!.status = 'stopping'
    expect(store.isGenerating).toBe(true)

    store.streaming!.status = 'error'
    expect(store.isGenerating).toBe(false)
  })

  it('does not ask the backend to stop twice', async () => {
    let calls = 0
    server.use(
      http.post(`${BASE_URL}/api/v1/conversations/${C1}/messages/${M1}/stop`, () => {
        calls += 1
        return HttpResponse.json({}, { status: 202 })
      }),
    )
    const store = streamingStore()

    await store.stopStreaming()
    await store.stopStreaming()

    expect(calls).toBe(1)
  })
})

describe('buffer 屬於哪一則', () => {
  /**
   * 一個 buffer、很多個對話：A 生成中切到 B，A 那條連線的事件仍會到（後端不因為
   * client 離開而停止，06 §4 的 G-06）。誰先到誰贏的話，A 的字會長在 B 的回答裡。
   */
  it('drops events addressed to a message that is no longer buffered', () => {
    const store = useChatStore()
    store.currentConversationId = C1
    store.beginStreaming({ messageId: M1, conversationId: C1 })
    store.appendDelta(M1, 'A 的字')

    // 切到 B，B 也有一則在生成 → buffer 換人。
    store.beginStreaming({ messageId: M2, conversationId: C2 })
    store.currentConversationId = C2

    store.appendDelta(M1, 'A 遲到的尾巴')
    store.applyCitations(M1, [{ marker: '1', doc_name: 'a.pdf' }])
    store.applyUsage(M1, { completion_tokens: 99 })
    store.failStreaming(M1, { code: 'X', title: 'x', retryable: false })

    expect(store.streaming).toMatchObject({ messageId: M2, text: '', status: 'streaming' })
    expect(store.streaming?.citations).toEqual([])
    expect(store.streaming?.usage).toBeNull()
  })

  it('does not swap the visible conversation when another one finishes', async () => {
    // `fetchMessages` 會連 `currentConversationId` 一起換掉：在看 B 的時候讓 A 的
    // 完成觸發它，畫面上的訊息整包變成 A 的內容，而它是最新的 requestId，守門機制
    // 反而站在它那邊。
    let fetchedA = false
    server.use(
      http.get(`${BASE_URL}/api/v1/conversations/${C1}/messages`, () => {
        fetchedA = true
        return HttpResponse.json({ items: [message('m-a', 'assistant', 'A 的回答')], next_cursor: null })
      }),
    )
    const store = useChatStore()
    store.beginStreaming({ messageId: M1, conversationId: C1 })
    // 使用者切到 B
    store.currentConversationId = C2
    store.messages = [message('m-b', 'assistant', 'B 的回答')]

    await store.finishStreaming(M1)

    expect(fetchedA).toBe(false)
    expect(store.currentConversationId).toBe(C2)
    expect(store.messages.map((m) => m.content)).toEqual(['B 的回答'])
    // 但 A 的 buffer 要收掉：它已經有結局了，留著只會被下一頁誤認成生成中。
    expect(store.streaming).toBeNull()
  })

  it('still refetches when the finished stream is the one on screen', async () => {
    server.use(
      http.get(`${BASE_URL}/api/v1/conversations/${C1}/messages`, () =>
        HttpResponse.json({ items: [message(M1, 'assistant', '完整的回答')], next_cursor: null }),
      ),
    )
    const store = useChatStore()
    store.currentConversationId = C1
    store.beginStreaming({ messageId: M1, conversationId: C1 })

    await store.finishStreaming(M1)

    expect(store.streaming).toBeNull()
    expect(store.messages.at(-1)).toMatchObject({ content: '完整的回答' })
  })

  it('keeps another conversation buffer when this one is deleted', async () => {
    server.use(
      http.delete(
        `${BASE_URL}/api/v1/conversations/${C1}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const store = useChatStore()
    store.conversations = [conversation(C1, '要刪的'), conversation(C2, '還在生成的')]
    store.currentConversationId = C1
    store.beginStreaming({ messageId: M2, conversationId: C2 })

    await store.deleteConversation(C1)

    expect(store.streaming?.conversationId).toBe(C2)
  })

  it('clears the buffer when its own conversation is deleted', async () => {
    server.use(
      http.delete(
        `${BASE_URL}/api/v1/conversations/${C1}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const store = useChatStore()
    store.conversations = [conversation(C1, '要刪的')]
    store.currentConversationId = C2
    store.beginStreaming({ messageId: M1, conversationId: C1 })

    await store.deleteConversation(C1)

    expect(store.streaming).toBeNull()
  })
})

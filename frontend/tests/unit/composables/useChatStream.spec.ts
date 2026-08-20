/**
 * 驗收：composables/useChatStream.ts（1E-3；03 §3.2）。
 *
 * 它是 SSE 與 store 之間唯一的接線：事件進來、狀態出去。元件因此完全不碰串流細節
 * （03 §1.4：SSE 邏輯集中在一個 composable + service）。
 *
 * 三個結局各自要走不同的路，而走錯了都不會報錯，只會讓畫面停在一個假的狀態：
 *
 * - `done`：以 server 的最終訊息覆蓋 buffer。
 * - `error` 事件：保留已收到的字，標成失敗（它是內容，不是傳輸故障）。
 * - 續傳過期 / 連不上：buffer 沒有結局可言，只能改抓最終訊息或告訴使用者連不上。
 */
import { createPinia, setActivePinia } from 'pinia'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { effectScope } from 'vue'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'

import { useChatStream } from '@/composables/useChatStream'
import { useChatStore } from '@/stores/chat'

const BASE_URL = 'http://api.test'
const C1 = '3f9f2b1e-0000-4000-8000-0000000000c1'
const M1 = 'm-assistant'
const STREAM_PATH = `/api/v1/conversations/${C1}/messages/${M1}/stream`
const server = setupServer()

const frame = (id: number, type: string, data: unknown): string =>
  `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify(data)}\n\n`

function sseResponse(chunks: string[]): HttpResponse<ReadableStream<Uint8Array>> {
  const encoder = new TextEncoder()
  const stream = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk))
      }
      controller.close()
    },
  })
  return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream' } })
}

const finalMessages = (content: string, citations: unknown[] = []) =>
  http.get(`${BASE_URL}/api/v1/conversations/${C1}/messages`, () =>
    HttpResponse.json({
      items: [
        {
          id: M1,
          role: 'assistant',
          content,
          citations,
          model: 'mock',
          status: 'complete',
          usage: {},
          created_at: '2026-08-20T00:00:00Z',
        },
      ],
      next_cursor: null,
    }),
  )

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
beforeEach(() => setActivePinia(createPinia()))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

/** 在 effect scope 裡建立（元件的等價物），並拿得到「卸載」的手把。 */
function inScope<T>(build: () => T): { value: T; dispose: () => void } {
  const scope = effectScope()
  const value = scope.run(build) as T
  return { value, dispose: () => scope.stop() }
}

function startedStore() {
  const store = useChatStore()
  store.currentConversationId = C1
  store.beginStreaming({ messageId: M1, conversationId: C1 })
  return store
}

describe('事件 → 狀態', () => {
  it('feeds deltas, citations and usage into the store', async () => {
    server.use(
      http.get(`${BASE_URL}${STREAM_PATH}`, () =>
        sseResponse([
          frame(1, 'meta', { message_id: M1, model: 'mock-chat', conversation_id: C1 }),
          frame(2, 'delta', { text: '年假' }),
          frame(3, 'delta', { text: '是 14 天[c:1]' }),
          frame(4, 'citations', { items: [{ marker: '1', doc_name: '人事規章.pdf' }] }),
          frame(5, 'usage', { prompt_tokens: 12, completion_tokens: 8, cost: null }),
          frame(6, 'done', { message_id: M1, finish_reason: 'stop' }),
        ]),
      ),
      finalMessages('年假是 14 天[c:1]', [{ marker: '1', doc_name: '人事規章.pdf' }]),
    )
    const store = startedStore()
    const { value: stream } = inScope(() => useChatStream())

    await stream.start({ conversationId: C1, messageId: M1 })

    // done 之後 buffer 讓位給後端的最終訊息（03 §3.2）。
    expect(store.streaming).toBeNull()
    expect(store.messages.at(-1)).toMatchObject({ id: M1, content: '年假是 14 天[c:1]' })
  })

  it('exposes the streaming text while it is still arriving', async () => {
    // 這是這一頁存在的理由：字要一段一段出現。整段等完才顯示的話，
    // 使用者會盯著一個不動的畫面十幾秒。
    const seen: string[] = []
    server.use(
      http.get(`${BASE_URL}${STREAM_PATH}`, () =>
        sseResponse([
          frame(1, 'delta', { text: '第一段' }),
          frame(2, 'delta', { text: '第二段' }),
          frame(3, 'done', { finish_reason: 'stop' }),
        ]),
      ),
      finalMessages('第一段第二段'),
    )
    const store = startedStore()
    // flush: 'sync' —— 預設的批次更新會把同一個 tick 的多次變動併成一次通知，
    // 而「逐段出現」正是這條測試要驗的東西。
    store.$subscribe(
      () => {
        if (store.streaming !== null) {
          seen.push(store.streaming.text)
        }
      },
      { flush: 'sync' },
    )
    const { value: stream } = inScope(() => useChatStream())

    await stream.start({ conversationId: C1, messageId: M1 })

    expect(seen).toContain('第一段')
    expect(seen).toContain('第一段第二段')
  })

  it('marks the buffer failed on an error event and keeps the partial text', async () => {
    server.use(
      http.get(`${BASE_URL}${STREAM_PATH}`, () =>
        sseResponse([
          frame(1, 'delta', { text: '講到一半' }),
          frame(2, 'error', {
            code: 'PROVIDER_UNAVAILABLE',
            title: '模型暫時不可用',
            retryable: true,
          }),
        ]),
      ),
    )
    const store = startedStore()
    const { value: stream } = inScope(() => useChatStream())

    await stream.start({ conversationId: C1, messageId: M1 })

    expect(store.streaming?.text).toBe('講到一半')
    expect(store.streaming?.status).toBe('error')
    expect(store.streaming?.error).toMatchObject({ code: 'PROVIDER_UNAVAILABLE' })
  })
})

describe('接不回去的時候', () => {
  it('falls back to the final message when the resume buffer expired', async () => {
    // 409 RESUME_EXPIRED（TTL 5 分鐘）。重連沒有意義，但答案其實已經生成完了
    // ——把它抓回來，使用者根本不需要知道中間發生過什麼。
    server.use(
      http.get(`${BASE_URL}${STREAM_PATH}`, () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'RESUME_EXPIRED',
            status: 409,
            detail: '續傳已過期',
            code: 'RESUME_EXPIRED',
            request_id: 'r1',
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
      finalMessages('這是完整的回答'),
    )
    const store = startedStore()
    const { value: stream } = inScope(() => useChatStream())

    await stream.start({ conversationId: C1, messageId: M1, lastEventId: '3' })

    expect(store.streaming).toBeNull()
    expect(store.messages.at(-1)).toMatchObject({ content: '這是完整的回答' })
  })

  it('tells the user when it cannot connect at all', async () => {
    // 連不上與「模型出錯」要分得出來：前者重新整理可能就好了，後者不會。
    server.use(http.get(`${BASE_URL}${STREAM_PATH}`, () => HttpResponse.error()))
    const store = startedStore()
    const { value: stream } = inScope(() => useChatStream())

    await stream.start({ conversationId: C1, messageId: M1, retryBaseMs: 1, maxRetries: 1 })

    expect(store.streaming?.status).toBe('error')
    expect(store.streaming?.error?.retryable).toBe(true)
  })
})

describe('生命週期', () => {
  it('reports whether a stream is open', async () => {
    server.use(
      http.get(`${BASE_URL}${STREAM_PATH}`, () =>
        sseResponse([frame(1, 'done', { finish_reason: 'stop' })]),
      ),
      finalMessages('完成'),
    )
    startedStore()
    const { value: stream } = inScope(() => useChatStream())

    expect(stream.isStreaming.value).toBe(false)
    const pending = stream.start({ conversationId: C1, messageId: M1 })
    expect(stream.isStreaming.value).toBe(true)
    await pending
    expect(stream.isStreaming.value).toBe(false)
  })

  it('aborts the connection when the component goes away', async () => {
    // 沒有這條的話，離開對話頁之後那條連線還開著，事件還在往一個沒人看的 store 寫。
    //
    // 串流**刻意不關閉**（後端還在生成的等價情境）：關掉的話它會自己結束，
    // 那就驗不到「是我們主動切斷的」。dispose 之前先等一個 tick，確保請求
    // 真的已經到達伺服器——否則測的會是「還沒送出就取消」，那是另一回事。
    let aborted = false
    server.use(
      http.get(`${BASE_URL}${STREAM_PATH}`, ({ request }) => {
        request.signal.addEventListener('abort', () => {
          aborted = true
        })
        const encoder = new TextEncoder()
        return new HttpResponse(
          new ReadableStream({
            start(controller) {
              controller.enqueue(encoder.encode(frame(1, 'delta', { text: '一' })))
              // 不 close：連線維持開著。
            },
          }),
          { headers: { 'Content-Type': 'text/event-stream' } },
        )
      }),
    )
    const store = startedStore()
    const { value: stream, dispose } = inScope(() => useChatStream())

    const pending = stream.start({ conversationId: C1, messageId: M1, retryBaseMs: 1 })
    await new Promise((resolve) => setTimeout(resolve, 20))
    dispose()
    await pending

    expect(aborted).toBe(true)
    expect(stream.isStreaming.value).toBe(false)
    // 已經收到的字留著——中斷不是失敗，使用者讀到的東西不該被沒收。
    expect(store.streaming?.text).toBe('一')
  })
})

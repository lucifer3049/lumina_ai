/**
 * 驗收：api/sse.ts 的傳輸層（1E-3；09 §3.2、03 §3.2、§3.3）。
 *
 * 解析交給 `parseSseStream`（見 sse-parser.spec.ts），這裡驗的是連線本身的四件事：
 *
 * 1. **憑證**：Bearer header 與 401 refresh 必須與一般請求走**同一套**（03 §3.3）。
 *    另寫一份的話，串流會在 access token 過期的那一刻死掉，而畫面上是「回答到一半
 *    突然停住」——沒有錯誤訊息，因為 401 發生在重連時。
 * 2. **續傳**：重連要帶 `Last-Event-ID`（09 §3.2），否則後端從頭送，畫面上整段回答
 *    會重複一次。
 * 3. **知道什麼時候不該重連**：`done`／`error` 是終局；409 RESUME_EXPIRED 代表緩衝區
 *    過期，重連幾次都一樣，client 該改抓最終訊息。
 * 4. **停得掉**：使用者離開頁面時連線要真的關掉，而不是留著跑到後端講完。
 */
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { configureAuth } from '@/api/client'
import { openEventStream, type SseEvent } from '@/api/sse'

const BASE_URL = 'http://api.test'
const PATH = '/api/v1/conversations/c1/messages/m1/stream'
const server = setupServer()

/** 一條照著後端格式寫的串流；`chunks` 之間會實際分次送達。 */
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
  return new HttpResponse(stream, {
    headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
  })
}

const frame = (id: number, type: string, data: unknown): string =>
  `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify(data)}\n\n`

const problem = (status: number, code: string) =>
  HttpResponse.json(
    { type: 'about:blank', title: code, status, detail: '失敗', code, request_id: 'req-1' },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  configureAuth(null)
})
afterAll(() => server.close())

/** 收集事件的小工具。 */
function collector(): { events: SseEvent[]; onEvent: (event: SseEvent) => void } {
  const events: SseEvent[] = []
  return { events, onEvent: (event) => events.push(event) }
}

describe('連線與憑證', () => {
  it('sends the bearer token and asks for an event stream', async () => {
    let authorization: string | null = null
    let accept: string | null = null
    server.use(
      http.get(`${BASE_URL}${PATH}`, ({ request }) => {
        authorization = request.headers.get('Authorization')
        accept = request.headers.get('Accept')
        return sseResponse([frame(1, 'done', { finish_reason: 'stop' })])
      }),
    )
    configureAuth({
      getToken: () => 'tok-1',
      onTokenRefreshed: () => {},
      onSessionExpired: () => {},
    })

    await openEventStream(PATH, collector())

    expect(authorization).toBe('Bearer tok-1')
    expect(accept).toBe('text/event-stream')
  })

  it('refreshes once and reconnects when the token expired', async () => {
    // 串流是長連線，最容易撞上 token 過期。refresh 必須走 client.ts 的 single-flight
    // ——自己打一次 refresh 的話，rotation 之下舊 token 被用第二次會撤銷整個家族
    // （等於自己把自己登出）。
    let attempts = 0
    server.use(
      http.get(`${BASE_URL}${PATH}`, () => {
        attempts += 1
        return attempts === 1
          ? problem(401, 'AUTH_TOKEN_EXPIRED')
          : sseResponse([frame(1, 'done', { finish_reason: 'stop' })])
      }),
      http.post(`${BASE_URL}/api/v1/auth/refresh`, () =>
        HttpResponse.json({ access_token: 'tok-2', token_type: 'Bearer', expires_in: 900 }),
      ),
    )
    let token = 'tok-1'
    const onTokenRefreshed = vi.fn((next: string) => {
      token = next
    })
    configureAuth({ getToken: () => token, onTokenRefreshed, onSessionExpired: () => {} })

    const outcome = await openEventStream(PATH, collector())

    expect(onTokenRefreshed).toHaveBeenCalledWith('tok-2')
    expect(attempts).toBe(2)
    expect(outcome.status).toBe('completed')
  })
})

describe('終局', () => {
  it('stops after a done event', async () => {
    server.use(
      http.get(`${BASE_URL}${PATH}`, () =>
        sseResponse([
          frame(1, 'meta', { message_id: 'm1', model: 'mock' }),
          frame(2, 'delta', { text: '你' }),
          frame(3, 'delta', { text: '好' }),
          frame(4, 'done', { message_id: 'm1', finish_reason: 'stop' }),
        ]),
      ),
    )
    const sink = collector()

    const outcome = await openEventStream(PATH, sink)

    expect(sink.events.map((event) => event.type)).toEqual(['meta', 'delta', 'delta', 'done'])
    expect(outcome.status).toBe('completed')
  })

  it('treats an error event as terminal without reconnecting', async () => {
    // `error` 事件的 HTTP 早就 200 了（09 §3.2）——它是內容，不是傳輸故障。
    // 當成斷線去重連的話，同一個錯誤會被重播好幾次。
    let attempts = 0
    server.use(
      http.get(`${BASE_URL}${PATH}`, () => {
        attempts += 1
        return sseResponse([
          frame(1, 'delta', { text: '半句' }),
          frame(2, 'error', {
            code: 'PROVIDER_UNAVAILABLE',
            title: '模型暫時不可用',
            retryable: true,
          }),
        ])
      }),
    )
    const sink = collector()

    const outcome = await openEventStream(PATH, sink, { retryBaseMs: 1 })

    expect(attempts).toBe(1)
    expect(outcome.status).toBe('completed')
    expect(sink.events.at(-1)?.type).toBe('error')
  })
})

describe('續傳（09 §3.2）', () => {
  it('reconnects with Last-Event-ID after the connection drops mid-stream', async () => {
    const seen: (string | null)[] = []
    let attempts = 0
    server.use(
      http.get(`${BASE_URL}${PATH}`, ({ request }) => {
        seen.push(request.headers.get('Last-Event-ID'))
        attempts += 1
        if (attempts === 1) {
          // 兩個事件之後直接斷掉（沒有 done）。
          return sseResponse([frame(1, 'delta', { text: '前' }), frame(2, 'delta', { text: '半' })])
        }
        return sseResponse([
          frame(3, 'delta', { text: '後半' }),
          frame(4, 'done', { finish_reason: 'stop' }),
        ])
      }),
    )
    const sink = collector()

    const outcome = await openEventStream(PATH, sink, { retryBaseMs: 1 })

    // 第一次不帶（從頭），第二次帶最後收到的編號——不帶的話後端從頭送，
    // 畫面上整段回答會重複一次。
    expect(seen).toEqual([null, '2'])
    expect(sink.events.filter((event) => event.type === 'delta')).toHaveLength(3)
    expect(outcome.status).toBe('completed')
  })

  it('starts from a caller-supplied Last-Event-ID', async () => {
    // 換頁回來、或元件重掛時，上層手上已經有收到哪裡了。
    let seen: string | null = null
    server.use(
      http.get(`${BASE_URL}${PATH}`, ({ request }) => {
        seen = request.headers.get('Last-Event-ID')
        return sseResponse([frame(9, 'done', { finish_reason: 'stop' })])
      }),
    )

    await openEventStream(PATH, collector(), { lastEventId: '8' })

    expect(seen).toBe('8')
  })

  it('reports resume-expired instead of retrying', async () => {
    // 409 RESUME_EXPIRED：緩衝區 TTL 5 分鐘已過（core/streams.py）。重連幾次都一樣，
    // client 該改抓最終訊息——所以要分得出這個結局。
    let attempts = 0
    server.use(
      http.get(`${BASE_URL}${PATH}`, () => {
        attempts += 1
        return problem(409, 'RESUME_EXPIRED')
      }),
    )

    const outcome = await openEventStream(PATH, collector(), { lastEventId: '3', retryBaseMs: 1 })

    expect(attempts).toBe(1)
    expect(outcome.status).toBe('resume-expired')
  })

  it('gives up after too many transport failures', async () => {
    let attempts = 0
    server.use(
      http.get(`${BASE_URL}${PATH}`, () => {
        attempts += 1
        return HttpResponse.error()
      }),
    )

    const outcome = await openEventStream(PATH, collector(), { retryBaseMs: 1, maxRetries: 2 })

    // 第一次 + 兩次重試。無限重連的頁面永遠不會告訴使用者「連不上」。
    expect(attempts).toBe(3)
    expect(outcome.status).toBe('failed')
  })
})

describe('取消', () => {
  it('closes the connection when the caller aborts', async () => {
    // 使用者離開頁面。留著連線的話，後端會一路講完，而那條連線也一直佔著。
    server.use(
      http.get(`${BASE_URL}${PATH}`, () => sseResponse([frame(1, 'delta', { text: '一' })])),
    )
    const controller = new AbortController()
    const sink = collector()

    const pending = openEventStream(PATH, sink, { signal: controller.signal, retryBaseMs: 1 })
    controller.abort()
    const outcome = await pending

    expect(outcome.status).toBe('aborted')
  })

  it('does not reconnect after abort', async () => {
    let attempts = 0
    server.use(
      http.get(`${BASE_URL}${PATH}`, () => {
        attempts += 1
        return HttpResponse.error()
      }),
    )
    const controller = new AbortController()

    const pending = openEventStream(PATH, collector(), {
      signal: controller.signal,
      retryBaseMs: 50,
      maxRetries: 5,
    })
    controller.abort()
    await pending

    expect(attempts).toBeLessThanOrEqual(1)
  })
})

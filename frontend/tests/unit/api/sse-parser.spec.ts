/**
 * 驗收：api/sse.ts 的線上格式解析（1E-3；09 §3.2、03 §3.2）。
 *
 * **不用 `EventSource`**（03 §1）：它帶不了 `Authorization` header，而本 API 的憑證
 * 是 Bearer。代價是 SSE 的斷句規則要自己實作，而那正是這支測試在盯的東西——它有
 * 三個「不會報錯、只會少字」的陷阱：
 *
 * 1. **事件會跨 chunk**：`ReadableStream` 的切割點與 SSE 的 `\n\n` 沒有任何關係。
 *    一個 chunk 一個事件地解析，在真實網路下會把事件切成兩半而安靜丟掉。
 * 2. **一個 chunk 可能有好幾個事件**：只取第一個的話，快速串流會少字。
 * 3. **註解行（心跳）不是事件**：`: heartbeat` 若被當成事件送出去，每 15 秒會多一個
 *    空的 delta。
 *
 * 解析器因此獨立於傳輸：這裡餵它自己造的 stream，不經過網路。
 */
import { describe, expect, it } from 'vitest'

import { parseSseStream, type SseEvent } from '@/api/sse'

/** 把字串陣列當成「網路上分次到達的 chunk」。 */
function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk))
      }
      controller.close()
    },
  })
}

async function collect(chunks: string[]): Promise<SseEvent[]> {
  const events: SseEvent[] = []
  for await (const event of parseSseStream(streamOf(chunks))) {
    events.push(event)
  }
  return events
}

describe('斷句', () => {
  it('parses a complete frame', async () => {
    const events = await collect(['id: 1\nevent: meta\ndata: {"model":"mock"}\n\n'])

    expect(events).toEqual([{ id: '1', type: 'meta', data: { model: 'mock' } }])
  })

  it('joins a frame split across chunks', async () => {
    // 這是最常見的真實情況：TCP 不管 SSE 的邊界在哪。
    const events = await collect(['id: 1\nevent: del', 'ta\ndata: {"text":"你好"}', '\n\n'])

    expect(events).toEqual([{ id: '1', type: 'delta', data: { text: '你好' } }])
  })

  it('reads several frames out of one chunk', async () => {
    const events = await collect([
      'id: 1\nevent: delta\ndata: {"text":"a"}\n\nid: 2\nevent: delta\ndata: {"text":"b"}\n\n',
    ])

    expect(events.map((event) => event.data)).toEqual([{ text: 'a' }, { text: 'b' }])
  })

  it('keeps newlines that are inside the payload', async () => {
    // 後端把 data 序列化成**一行 JSON**（api/sse.py）：回答裡的換行是 `\n` 跳脫序列，
    // 與協定的換行不是同一個東西。解析器把它還原回真正的換行。
    const events = await collect(['event: delta\ndata: {"text":"第一行\\n第二行"}\n\n'])

    expect((events[0]?.data as { text: string }).text).toBe('第一行\n第二行')
  })

  it('ignores heartbeat comments', async () => {
    // `: heartbeat` 是 SSE 的註解行（api/sse.py 的 format_heartbeat）。
    const events = await collect([
      ': heartbeat\n\n',
      'event: done\ndata: {"finish_reason":"stop"}\n\n',
    ])

    expect(events).toHaveLength(1)
    expect(events[0]?.type).toBe('done')
  })

  it('drops a trailing partial frame instead of emitting half an event', async () => {
    // 連線在事件中間斷掉：半個事件不可解析，送出去只會讓上層拿到殘缺的 JSON。
    // 真正該發生的是重連（由傳輸層負責），這裡只要不製造假事件。
    const events = await collect([
      'event: delta\ndata: {"text":"完整"}\n\n',
      'event: delta\ndata: {"te',
    ])

    expect(events).toHaveLength(1)
  })
})

describe('容錯', () => {
  it('skips a frame whose data is not JSON', async () => {
    // 代理伺服器塞進來的東西、或後端真的有 bug。丟掉那一個事件即可——
    // 讓整條串流因為一個壞事件而中止，使用者會看到回答無故停在半路。
    const events = await collect([
      'event: delta\ndata: not-json\n\n',
      'event: delta\ndata: {"text":"後續照收"}\n\n',
    ])

    expect(events).toHaveLength(1)
    expect((events[0]?.data as { text: string }).text).toBe('後續照收')
  })

  it('defaults the event name to "message" when the frame has none', async () => {
    // SSE 規範的預設事件名。後端一定會給 `event:`，但代理或未來的端點不保證。
    const events = await collect(['data: {"text":"x"}\n\n'])

    expect(events[0]?.type).toBe('message')
  })

  it('tolerates CRLF line endings', async () => {
    const events = await collect(['id: 7\r\nevent: delta\r\ndata: {"text":"x"}\r\n\r\n'])

    expect(events[0]).toEqual({ id: '7', type: 'delta', data: { text: 'x' } })
  })
})

/**
 * SSE client（1E-3；09 §3.2、03 §1.4、§3.2）。
 *
 * **不用 `EventSource`**：它帶不了自訂 header，而本 API 的憑證是 `Authorization:
 * Bearer`（09 §1.2）。改用 fetch + ReadableStream 的代價是自動重連與 `Last-Event-ID`
 * 要自己維護——這個檔案就是那份代價，元件因此完全不碰串流細節。
 *
 * 兩層分開：
 *
 * - `parseSseStream()`：位元組 → 事件。純粹、可單獨測，不知道 HTTP 的存在。
 * - `openEventStream()`：連線、認證、重連、續傳、終局判定。
 */
import { ApiError, authorizedFetch } from '@/api/client'

/** 一個 SSE 事件。`id` 是後端的事件編號（`Last-Event-ID` 用它續傳）。 */
export interface SseEvent {
  id?: string
  type: string
  data: unknown
}

/**
 * 位元組串流 → 事件。
 *
 * **緩衝區是關鍵**：`ReadableStream` 的切割點與 SSE 的 `\n\n` 沒有任何關係，一個
 * chunk 一個事件地解析，在真實網路下會把事件切成兩半而安靜丟掉（症狀是「偶爾少
 * 一段字」）。收尾時**刻意丟掉**殘留的半個事件：那是連線中斷，該做的是重連，不是
 * 送出一個殘缺的 JSON。
 */
export async function* parseSseStream(
  stream: ReadableStream<Uint8Array>,
  options: { signal?: AbortSignal } = {},
): AsyncGenerator<SseEvent> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    for (;;) {
      const result = await readOrAbort(reader, options.signal)
      if (result === ABORTED || result.done) {
        break
      }
      buffer += decoder.decode(result.value, { stream: true })

      // 事件之間以空白行分隔；CRLF 與 LF 都要認（代理可能改寫換行）。
      let boundary = findBoundary(buffer)
      while (boundary !== null) {
        const frame = buffer.slice(0, boundary.index)
        buffer = buffer.slice(boundary.index + boundary.length)
        const event = parseFrame(frame)
        if (event !== null) {
          yield event
        }
        boundary = findBoundary(buffer)
      }
    }
  } finally {
    // 讀到一半就離開（呼叫端 break、或上層 abort）時要主動取消，
    // 否則連線會留著直到 GC——而 GC 的時機沒有人保證。
    // `cancel()` 而不是 `releaseLock()`：還有 pending 的 read 時後者會丟 TypeError。
    void reader.cancel().catch(() => {})
  }
}

/** `read()` 被 abort 打斷時的哨兵值。 */
const ABORTED = Symbol('aborted')

/**
 * 等下一批位元組，但**別等到天荒地老**。
 *
 * 取消一條 fetch 之後，body 的 `read()` 不保證會被拒絕——攔截層（測試用的 msw）與
 * 某些代理下它就是一直不回來。少了這一層，元件卸載之後那個 promise 會永遠掛著，
 * 而「離開頁面就斷線」實際上沒有發生。
 */
async function readOrAbort(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  signal: AbortSignal | undefined,
): Promise<ReadableStreamReadResult<Uint8Array> | typeof ABORTED> {
  if (signal === undefined) {
    return reader.read()
  }
  if (signal.aborted) {
    return ABORTED
  }
  const cleanup = new AbortController()
  try {
    return await Promise.race([
      reader.read(),
      new Promise<typeof ABORTED>((resolve) => {
        signal.addEventListener('abort', () => resolve(ABORTED), { signal: cleanup.signal })
      }),
    ])
  } finally {
    // 讀取贏了的時候要把 listener 拆掉，否則每一批位元組都留一個在 signal 上。
    cleanup.abort()
  }
}

function findBoundary(buffer: string): { index: number; length: number } | null {
  const lf = buffer.indexOf('\n\n')
  const crlf = buffer.indexOf('\r\n\r\n')
  if (crlf !== -1 && (lf === -1 || crlf < lf)) {
    return { index: crlf, length: 4 }
  }
  return lf === -1 ? null : { index: lf, length: 2 }
}

/** 一段文字 → 事件；不是事件（心跳註解、無 data、data 不是 JSON）時回 null。 */
function parseFrame(frame: string): SseEvent | null {
  let id: string | undefined
  let type = ''
  const dataLines: string[] = []

  for (const line of frame.split(/\r?\n/)) {
    // `:` 開頭是註解行——心跳走的就是這條（api/sse.py 的 format_heartbeat）。
    // 當成事件送出去的話，每 15 秒會多一個空的 delta。
    if (line === '' || line.startsWith(':')) {
      continue
    }
    const separator = line.indexOf(':')
    const field = separator === -1 ? line : line.slice(0, separator)
    // 規範允許欄位值前面有一個空格，那個空格不算內容。
    const rawValue = separator === -1 ? '' : line.slice(separator + 1)
    const value = rawValue.startsWith(' ') ? rawValue.slice(1) : rawValue

    if (field === 'id') {
      id = value
    } else if (field === 'event') {
      type = value
    } else if (field === 'data') {
      dataLines.push(value)
    }
    // 其餘欄位（`retry` 等）忽略：後端不送，而猜測它們的語意只會製造行為差異。
  }

  if (dataLines.length === 0) {
    return null
  }

  let data: unknown
  try {
    data = JSON.parse(dataLines.join('\n'))
  } catch {
    // 壞掉的事件只丟這一個。讓整條串流因為它中止的話，畫面上是回答無故停在半路。
    return null
  }

  // 沒有 `event:` 時用 SSE 規範的預設名。後端一定會給，代理不保證。
  return { ...(id === undefined ? {} : { id }), type: type === '' ? 'message' : type, data }
}

// ── 連線 ────────────────────────────────────────────────────────────────

export interface SseHandlers {
  /** 每一個事件。回傳 promise 的話會被等待——`done` 要先寫完 store 再收線。 */
  onEvent: (event: SseEvent) => void | Promise<void>
  /** 每一次傳輸層失敗（會重連）。純粹給診斷用，不必接。 */
  onError?: (error: unknown) => void
}

export interface SseOptions {
  /** 從哪個事件編號之後續傳（09 §3.2）。 */
  lastEventId?: string | null
  /** 傳輸失敗的重試次數上限，預設 3。 */
  maxRetries?: number
  /** 退讓的基數，預設 500ms（第 n 次重試等 base × 2^(n-1)）。 */
  retryBaseMs?: number
  signal?: AbortSignal
}

export interface SseOutcome {
  /**
   * - `completed`：收到 `done` 或 `error` 事件（兩者都是後端說完了）。
   * - `aborted`：呼叫端自己取消。
   * - `resume-expired`：緩衝區過期（409），重連沒有意義，改抓最終訊息。
   * - `failed`：連不上且重試用盡。
   */
  status: 'completed' | 'aborted' | 'resume-expired' | 'failed'
  /** 最後收到的事件編號——上層要接著續傳時用得上。 */
  lastEventId: string | null
  error?: unknown
}

/** `done`／`error` 之後不再讀：那兩個是後端的收線訊號（api/v1/conversations.py 同一份判斷）。 */
const TERMINAL_EVENTS = new Set(['done', 'error'])

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * 開一條 SSE 連線並把事件交給 handler，直到終局為止。
 *
 * 重連的判斷分成三種，混在一起就會出錯：
 *
 * 1. **`error` 事件不是傳輸故障**——HTTP 早就 200 了（09 §3.2），它是內容。
 *    當成斷線重連的話，同一個錯誤會被重播好幾次。
 * 2. **串流沒有終局事件就結束 = 斷線**，要帶 `Last-Event-ID` 重連（不帶的話後端
 *    從頭送，畫面上整段回答重複一次）。
 * 3. **409 RESUME_EXPIRED 不重試**：緩衝區 TTL 5 分鐘已過，重連幾次都一樣。
 */
export async function openEventStream(
  path: string,
  handlers: SseHandlers,
  options: SseOptions = {},
): Promise<SseOutcome> {
  const { signal, maxRetries = 3, retryBaseMs = 500 } = options
  let lastEventId = options.lastEventId ?? null
  let attempt = 0

  // 函式而不是直接讀 `signal.aborted`：TS 會把第一次判斷之後的值窄化成 false
  // （它不知道那個旗標會被外面改），於是後面兩次檢查被判定成「不可能成立」。
  const isAborted = (): boolean => signal?.aborted ?? false

  for (;;) {
    if (isAborted()) {
      return { status: 'aborted', lastEventId }
    }

    try {
      const response = await authorizedFetch(path, {
        headers: {
          Accept: 'text/event-stream',
          ...(lastEventId === null ? {} : { 'Last-Event-ID': lastEventId }),
        },
        signal,
      })

      if (response.body !== null) {
        for await (const event of parseSseStream(response.body, { signal })) {
          if (event.id !== undefined) {
            lastEventId = event.id
          }
          await handlers.onEvent(event)
          if (TERMINAL_EVENTS.has(event.type)) {
            return { status: 'completed', lastEventId }
          }
        }
      }
      // 走到這裡＝串流結束但沒有終局事件：不是被取消（下一行），就是連線掉了 → 續傳。
      if (isAborted()) {
        return { status: 'aborted', lastEventId }
      }
    } catch (error) {
      if (isAborted()) {
        return { status: 'aborted', lastEventId }
      }
      if (error instanceof ApiError && error.code === 'RESUME_EXPIRED') {
        return { status: 'resume-expired', lastEventId, error }
      }
      handlers.onError?.(error)
      if (attempt >= maxRetries) {
        return { status: 'failed', lastEventId, error }
      }
      attempt += 1
      await sleep(retryBaseMs * 2 ** (attempt - 1))
      continue
    }

    if (attempt >= maxRetries) {
      return { status: 'failed', lastEventId }
    }
    attempt += 1
    await sleep(retryBaseMs * 2 ** (attempt - 1))
  }
}

/**
 * SSE 與 store 之間唯一的接線（1E-3；03 §1.4、§3.2）。
 *
 * 元件只呼叫 `start()` 與 `stop()`，不知道事件、續傳或重連的存在。
 *
 * 三個結局各走各的路，而走錯了都不會報錯、只會讓畫面停在一個假的狀態：
 *
 * - `done` 事件 → 以 server 的最終訊息覆蓋 buffer。
 * - `error` 事件 → 保留已收到的字並標成失敗（它是內容，不是傳輸故障）。
 * - 續傳過期 / 連不上 → buffer 沒有結局可言：前者改抓最終訊息（答案其實已經生成
 *   完了，使用者根本不需要知道中間發生過什麼），後者只能告訴使用者連不上。
 *
 * **同一時間只准有一條連線**。`onScopeDispose` 只在元件卸載時觸發，而 `/chat` ↔
 * `/chat/:id` 的切換不卸載本元件——舊的 controller 被 `start()` 覆蓋掉的話，那條連線
 * 還活著而且沒有人斷得了它，兩條串流會同時往同一個 buffer 寫字。所以開新的之前先
 * 斷舊的；後端的生成不因此停止（06 §4 的 G-06），切回去時 `resumeUnfinished` 會接。
 */
import { onScopeDispose, ref, type Ref } from 'vue'

import { openEventStream, type SseEvent } from '@/api/sse'
import { useChatStore } from '@/stores/chat'
import type { CitationItem } from '@/utils/citations'

export interface StartOptions {
  conversationId: string
  messageId: string
  /** 續傳的起點（換頁回來時上層手上已經有收到哪裡了）。 */
  lastEventId?: string | null
  retryBaseMs?: number
  maxRetries?: number
}

export interface ChatStream {
  isStreaming: Ref<boolean>
  start: (options: StartOptions) => Promise<void>
  stop: () => void
}

/** 傳輸層自己的錯誤碼——**不屬於** 09 附錄 A 的契約字典，後端不會回這個值。 */
const DISCONNECTED: { code: string; title: string; retryable: boolean } = {
  code: 'STREAM_DISCONNECTED',
  title: '與伺服器的連線中斷',
  retryable: true,
}

const asRecord = (value: unknown): Record<string, unknown> =>
  typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : {}

const asText = (value: unknown): string => (typeof value === 'string' ? value : '')

export function useChatStream(): ChatStream {
  const store = useChatStore()
  const isStreaming = ref(false)
  let controller: AbortController | null = null
  /**
   * 哪一次 `start()` 才有資格動共用狀態。abort 之後仍可能有一個已經進到 handler 的
   * 事件在飛，而舊的 `finally` 若晚一步跑完，就會把新串流的 `isStreaming` 關掉、
   * 把新的 controller 設成 null——症狀是「停止鈕消失了但字還在跑」。
   */
  let runId = 0

  async function start(options: StartOptions): Promise<void> {
    controller?.abort()
    const ownController = new AbortController()
    const ownRun = (runId += 1)
    controller = ownController
    isStreaming.value = true

    // store 的寫入一律指名 messageId：這條連線只准寫自己那一則的 buffer。
    const onEvent = async (event: SseEvent): Promise<void> => {
      if (ownRun !== runId) {
        return // 已經被新的串流取代，這是遲到的事件
      }
      const data = asRecord(event.data)
      switch (event.type) {
        case 'delta':
          store.appendDelta(options.messageId, asText(data.text))
          break
        case 'citations':
          store.applyCitations(
            options.messageId,
            Array.isArray(data.items) ? (data.items as CitationItem[]) : [],
          )
          break
        case 'usage':
          store.applyUsage(options.messageId, data)
          break
        case 'done':
          // await：收線之前要先把最終訊息換上去，否則 start() 回來時畫面還是舊的。
          await store.finishStreaming(options.messageId)
          break
        case 'error':
          store.failStreaming(options.messageId, {
            code: asText(data.code) || 'INTERNAL_ERROR',
            title: asText(data.title) || '生成失敗',
            retryable: data.retryable === true,
          })
          break
        default:
          // `meta` 目前沒有畫面要用的東西；`tool_call` 等 3A 才有真的工具（決定記於
          // 1E-3 開工討論）。未知事件一律安靜略過——後端加新事件不該讓前端壞掉。
          break
      }
    }

    try {
      const outcome = await openEventStream(
        `/api/v1/conversations/${encodeURIComponent(options.conversationId)}/messages/${encodeURIComponent(options.messageId)}/stream`,
        { onEvent },
        {
          lastEventId: options.lastEventId ?? null,
          retryBaseMs: options.retryBaseMs,
          maxRetries: options.maxRetries,
          signal: ownController.signal,
        },
      )

      if (ownRun !== runId) {
        return // 結局也一樣：被取代的串流不得再動 store
      }
      if (outcome.status === 'resume-expired') {
        // 生成本身沒有失敗，只是我們接不回那條串流了。
        await store.finishStreaming(options.messageId)
      } else if (outcome.status === 'failed') {
        store.failStreaming(options.messageId, DISCONNECTED)
      }
      // `completed`：done／error 事件已經處理過了。
      // `aborted`：使用者自己離開或按停止，buffer 維持現狀。
    } finally {
      if (ownRun === runId) {
        isStreaming.value = false
        controller = null
      }
    }
  }

  /** 只切斷本地連線，不通知後端——後端要停生成走 `store.stopStreaming()`（09 §2.4）。 */
  function stop(): void {
    controller?.abort()
  }

  // 元件卸載即斷線。沒有這條的話，離開對話頁之後連線還開著，
  // 事件還在往一個沒人看的 store 寫。
  onScopeDispose(stop, true)

  return { isStreaming, start, stop }
}

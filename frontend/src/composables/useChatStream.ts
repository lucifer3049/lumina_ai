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

  async function onEvent(event: SseEvent): Promise<void> {
    const data = asRecord(event.data)
    switch (event.type) {
      case 'delta':
        store.appendDelta(asText(data.text))
        break
      case 'citations':
        store.applyCitations(Array.isArray(data.items) ? (data.items as CitationItem[]) : [])
        break
      case 'usage':
        store.applyUsage(data)
        break
      case 'done':
        // await：收線之前要先把最終訊息換上去，否則 start() 回來時畫面還是舊的。
        await store.finishStreaming()
        break
      case 'error':
        store.failStreaming({
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

  async function start(options: StartOptions): Promise<void> {
    controller = new AbortController()
    isStreaming.value = true
    try {
      const outcome = await openEventStream(
        `/api/v1/conversations/${encodeURIComponent(options.conversationId)}/messages/${encodeURIComponent(options.messageId)}/stream`,
        { onEvent },
        {
          lastEventId: options.lastEventId ?? null,
          retryBaseMs: options.retryBaseMs,
          maxRetries: options.maxRetries,
          signal: controller.signal,
        },
      )

      if (outcome.status === 'resume-expired') {
        // 生成本身沒有失敗，只是我們接不回那條串流了。
        await store.finishStreaming()
      } else if (outcome.status === 'failed') {
        store.failStreaming(DISCONNECTED)
      }
      // `completed`：done／error 事件已經處理過了。
      // `aborted`：使用者自己離開或按停止，buffer 維持現狀。
    } finally {
      isStreaming.value = false
      controller = null
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

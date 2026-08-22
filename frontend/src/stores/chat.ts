/**
 * 對話狀態（1E-3；03 §2、09 §2.4、§3.2）。
 *
 * 這個 store 持有兩種東西，而它們的生命週期完全不同：
 *
 * - **已定案的訊息**（`messages`）：後端是唯一真相。
 * - **正在生成的那一則**（`streaming`）：只活在記憶體，由 SSE 事件一段一段長出來。
 *
 * 交接點是 `done`——`finishStreaming()` **先抓後清**（03 §3.2 的 Single Source of
 * Truth）。反過來畫面會空一下（回答消失又出現）；而直接把 buffer 當成定案訊息的話，
 * 內容、`usage` 與後端實際存的可能有出入，那正是 1D-4a 的不變式要驗的東西。
 *
 * 事件的接線在 `composables/useChatStream.ts`——這裡只有狀態與 API，不知道 SSE 存在。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { request } from '@/api/client'
import type {
  ConversationCreateIn,
  ConversationListOut,
  ConversationOut,
  ConversationUpdateIn,
  MessageListOut,
  MessageOut,
  TurnStartedOut,
} from '@/types/models'
import type { CitationItem } from '@/utils/citations'

const JSON_HEADERS = { 'Content-Type': 'application/json' } as const

/** 生成中那一則回答的狀態。`stopping` 是「已受理停止、還沒真的停」（09 §3.3 的 202）。 */
export type StreamingStatus = 'streaming' | 'stopping' | 'error'

export interface StreamError {
  code: string
  title: string
  retryable: boolean
}

export interface StreamingMessage {
  messageId: string
  conversationId: string
  text: string
  citations: CitationItem[]
  usage: Record<string, unknown> | null
  status: StreamingStatus
  error: StreamError | null
}

export const useChatStore = defineStore('chat', () => {
  const conversations = ref<ConversationOut[]>([])
  const currentConversationId = ref<string | null>(null)
  /**
   * 首頁輸入列的草稿：在首頁打一句話 → 存這裡 → 導到 /chat，由對話頁取走並送出。
   * 走 store 而不是路由參數：問題內容可以很長，也不該出現在網址列與瀏覽紀錄。
   */
  const pendingDraft = ref<string | null>(null)
  const messages = ref<MessageOut[]>([])
  const streaming = ref<StreamingMessage | null>(null)
  const loadingConversations = ref(false)
  const loadingMessages = ref(false)

  /** 切對話是一秒內會做兩次的動作，慢的回應不得覆蓋後來的選擇（同 knowledge store）。 */
  let messagesRequestId = 0

  const isGenerating = computed(() => streaming.value?.status === 'streaming')

  // ── 對話 ────────────────────────────────────────────────────────────────

  async function fetchConversations(): Promise<void> {
    loadingConversations.value = true
    try {
      const page = await request<ConversationListOut>('/api/v1/conversations')
      conversations.value = page.items
    } finally {
      loadingConversations.value = false
    }
  }

  async function createConversation(input: ConversationCreateIn): Promise<ConversationOut> {
    const created = await request<ConversationOut>('/api/v1/conversations', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(input),
    })
    // 最前面：後端列表是最近的在前，加在最後一重新整理就跳位。
    conversations.value = [created, ...conversations.value]
    return created
  }

  async function updateConversation(
    conversationId: string,
    patch: ConversationUpdateIn,
  ): Promise<ConversationOut> {
    const updated = await request<ConversationOut>(
      `/api/v1/conversations/${encodeURIComponent(conversationId)}`,
      { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(patch) },
    )
    conversations.value = conversations.value.map((item) =>
      item.id === conversationId ? updated : item,
    )
    return updated
  }

  async function deleteConversation(conversationId: string): Promise<void> {
    await request<null>(`/api/v1/conversations/${encodeURIComponent(conversationId)}`, {
      method: 'DELETE',
    })
    conversations.value = conversations.value.filter((item) => item.id !== conversationId)
    if (currentConversationId.value === conversationId) {
      // 留著訊息的話，畫面上還開著一個不存在的對話，而送出下一句會 404。
      currentConversationId.value = null
      messages.value = []
      streaming.value = null
    }
  }

  // ── 訊息 ────────────────────────────────────────────────────────────────

  /** `silent` 給串流結束後的重抓用：翻動 loading 會讓整頁閃一次骨架畫面。 */
  async function fetchMessages(
    conversationId: string,
    options: { silent?: boolean } = {},
  ): Promise<void> {
    const silent = options.silent === true
    messagesRequestId += 1
    const requestId = messagesRequestId
    currentConversationId.value = conversationId
    if (!silent) {
      loadingMessages.value = true
    }
    try {
      // **時間正序**（09 §2.4）：1D-5 用同一條路徑組 context，前端不自己倒轉。
      const page = await request<MessageListOut>(
        `/api/v1/conversations/${encodeURIComponent(conversationId)}/messages`,
      )
      if (requestId !== messagesRequestId) {
        return // 使用者已經換到別的對話
      }
      messages.value = page.items
    } finally {
      if (requestId === messagesRequestId && !silent) {
        loadingMessages.value = false
      }
    }
  }

  /**
   * 送出一個問題（09 §2.4）。POST 只建立回合，生成跑在背景，串流是另一條路徑。
   *
   * 成功之後才動本地狀態：先樂觀塞一則使用者訊息、POST 再失敗的話，畫面上會留下
   * 一句沒有人回答的話，而重試會變成兩句。
   */
  async function sendMessage(conversationId: string, content: string): Promise<TurnStartedOut> {
    const turn = await request<TurnStartedOut>(
      `/api/v1/conversations/${encodeURIComponent(conversationId)}/messages`,
      { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ content }) },
    )

    // 使用者那句要立刻出現：等串流開始才顯示的話，送出後畫面有一段空白，
    // 而使用者會以為沒送出去。`created_at` 是本地時間，下一次重抓就會換成後端的。
    messages.value = [
      ...messages.value,
      {
        id: turn.user_message_id,
        role: 'user',
        content,
        citations: [],
        model: '',
        status: 'complete',
        usage: {},
        created_at: new Date().toISOString(),
      },
    ]
    beginStreaming({ messageId: turn.message_id, conversationId })
    return turn
  }

  // ── 生成中的那一則 ──────────────────────────────────────────────────────

  function beginStreaming(init: { messageId: string; conversationId: string }): void {
    streaming.value = {
      messageId: init.messageId,
      conversationId: init.conversationId,
      text: '',
      citations: [],
      usage: null,
      status: 'streaming',
      error: null,
    }
  }

  /** 事件可能在換頁之後才到；沒有 buffer 就是沒人在等它了。 */
  function appendDelta(text: string): void {
    if (streaming.value === null) {
      return
    }
    streaming.value.text += text
  }

  function applyCitations(items: readonly CitationItem[]): void {
    if (streaming.value === null) {
      return
    }
    streaming.value.citations = [...items]
  }

  function applyUsage(usage: Record<string, unknown>): void {
    if (streaming.value === null) {
      return
    }
    streaming.value.usage = { ...usage }
  }

  /** `done` 之後：先抓後清（見檔頭）。 */
  async function finishStreaming(): Promise<void> {
    const conversationId = streaming.value?.conversationId ?? currentConversationId.value
    if (conversationId !== null) {
      await fetchMessages(conversationId, { silent: true })
    }
    streaming.value = null
  }

  /**
   * `error` 事件或連不上。**保留已收到的字**：HTTP 早就 200 了，那些字是有效內容，
   * 清掉等於把使用者已經讀到的東西沒收。
   */
  function failStreaming(error: StreamError): void {
    if (streaming.value === null) {
      return
    }
    streaming.value.status = 'error'
    streaming.value.error = error
  }

  /** 請後端停止生成。202 = 已受理、還沒發生（真正停下來的是另一個行程裡的 task）。 */
  async function stopStreaming(): Promise<void> {
    const current = streaming.value
    if (current === null) {
      return
    }
    current.status = 'stopping'
    await request<null>(
      `/api/v1/conversations/${encodeURIComponent(current.conversationId)}/messages/${encodeURIComponent(current.messageId)}/stop`,
      { method: 'POST' },
    )
  }

  return {
    conversations,
    currentConversationId,
    pendingDraft,
    messages,
    streaming,
    loadingConversations,
    loadingMessages,
    isGenerating,
    fetchConversations,
    createConversation,
    updateConversation,
    deleteConversation,
    fetchMessages,
    sendMessage,
    beginStreaming,
    appendDelta,
    applyCitations,
    applyUsage,
    finishStreaming,
    failStreaming,
    stopStreaming,
  }
})

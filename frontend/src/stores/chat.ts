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
 *
 * **buffer 只有一個，而對話有很多個**：在 A 生成中切到 B，A 那條連線的事件仍會陸續
 * 到達（後端不因為 client 離開而停止生成，06 §4 的 G-06）。因此寫進 buffer 的每一個
 * 動作都要指名「這是哪一則回答的事件」——誰先到誰贏的話，A 的字會長在 B 的回答裡。
 * 誰在畫面上則由 `currentConversationId` 說了算，元件再依它決定要不要渲染 buffer。
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

  /**
   * `stopping` 也算生成中：停止是 202「已受理、還沒發生」，串流還沒收線。這裡放行的
   * 話輸入框立刻解鎖，使用者送下一句 → 新的 buffer 蓋掉舊的，而舊連線還在寫。
   */
  const isGenerating = computed(
    () => streaming.value?.status === 'streaming' || streaming.value?.status === 'stopping',
  )

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
    }
    // buffer 另外判斷：被刪的不一定是正在生成的那個，一律清等於把別人的字擦掉。
    if (streaming.value?.conversationId === conversationId) {
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
        status: 'completed',
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

  /**
   * 事件屬於哪一則由呼叫端指名——沒有 buffer（換頁之後才到）或 buffer 已經換成另一
   * 則（切到別的對話、送出下一句）時一律丟掉。少了 `messageId` 這道門，上一條串流
   * 的尾巴會接在新回答的開頭，而畫面上看起來只是「文字交錯」，沒有任何錯誤。
   */
  const bufferFor = (messageId: string): StreamingMessage | null =>
    streaming.value?.messageId === messageId ? streaming.value : null

  function appendDelta(messageId: string, text: string): void {
    const buffer = bufferFor(messageId)
    if (buffer !== null) {
      buffer.text += text
    }
  }

  function applyCitations(messageId: string, items: readonly CitationItem[]): void {
    const buffer = bufferFor(messageId)
    if (buffer !== null) {
      buffer.citations = [...items]
    }
  }

  function applyUsage(messageId: string, usage: Record<string, unknown>): void {
    const buffer = bufferFor(messageId)
    if (buffer !== null) {
      buffer.usage = { ...usage }
    }
  }

  /**
   * `done` 之後：先抓後清（見檔頭）。
   *
   * **只重抓畫面上的那個對話**。`fetchMessages` 會連 `currentConversationId` 一起
   * 換掉，所以在看 B 的時候讓 A 的完成觸發它，等於整包訊息被換成 A 的內容——而它是
   * 最新的 requestId，`fetchMessages` 的守門機制反而站在它那邊。不重抓不會漏東西：
   * 下次切回 A 時 watch 自己會抓。
   */
  async function finishStreaming(messageId: string): Promise<void> {
    const buffer = bufferFor(messageId)
    if (buffer === null) {
      return
    }
    const conversationId = buffer.conversationId
    if (conversationId === currentConversationId.value) {
      await fetchMessages(conversationId, { silent: true })
    }
    // 再確認一次：重抓期間使用者可能已經送出下一句，那是新的 buffer，不是我的。
    if (bufferFor(messageId) !== null) {
      streaming.value = null
    }
  }

  /**
   * `error` 事件或連不上。**保留已收到的字**：HTTP 早就 200 了，那些字是有效內容，
   * 清掉等於把使用者已經讀到的東西沒收。
   */
  function failStreaming(messageId: string, error: StreamError): void {
    const buffer = bufferFor(messageId)
    if (buffer === null) {
      return
    }
    buffer.status = 'error'
    buffer.error = error
  }

  /** 請後端停止生成。202 = 已受理、還沒發生（真正停下來的是另一個行程裡的 task）。 */
  async function stopStreaming(): Promise<void> {
    const current = streaming.value
    if (current === null || current.status !== 'streaming') {
      return // 已經受理過（或已經結束）——再送一次只是重複打後端。
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

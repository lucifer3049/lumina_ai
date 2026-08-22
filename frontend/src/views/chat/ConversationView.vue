<script setup lang="ts">
/**
 * 對話頁（ChatGPT 版式、青綠皮膚；09 §2.4、§3.2、03 §3.2）。
 *
 * 左＝對話側欄（搜尋＋日期分組），中＝訊息與輸入，右＝引用箋紙（有引用才攤開）。
 * `/chat`（無 conversationId）與 `/chat/:conversationId` 共用本元件：兩條路由切換
 * 只是 prop 變化，側欄不重掛、SSE（useChatStream 的 onScopeDispose）不會被切斷。
 *
 * 對話**在第一句送出時才建立**（同 ChatGPT）：先建後問的話，開了又不問的對話會
 * 在清單裡堆成一排（未命名）。自建後自己 push 路由，用 `selfNavigated` 讓 watch
 * 跳過那一次抓取——全新對話沒有歷史可抓，抓了反而跟樂觀塞入的那句賽跑。
 *
 * 送出與串流是**兩步**（1D-4a）：`sendMessage()` 建立回合並拿到 `message_id`，
 * 再用它開串流。合成一步的話，網路閃斷時使用者分不出送出去了沒，重送就是兩則
 * 訊息、兩次生成、兩次帳單。
 *
 * 重新整理（或深層連結）進來時，**沒讀完的那則會自己接回去**：訊息清單裡若有一則
 * 還在 `streaming`，就對它開一條串流——後端的生成不因為 client 離開而停止（06 §4
 * 的 G-06），所以接回去看到的是完整的後半段。
 *
 * `store.streaming` 是**全域單例**，而這個元件同時服務所有對話：畫面上的每一處都得
 * 先問「這條 buffer 是不是這個對話的」（`streamingHere`）。無條件渲染的話，A 生成中
 * 的回答會出現在 B 的畫面裡——而它連錯誤都不算，看起來只是「怎麼多了一段」。
 */
import { computed, nextTick, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import ChatComposer from '@/components/chat/ChatComposer.vue'
import CitationPanel from '@/components/chat/CitationPanel.vue'
import ConversationSidebar from '@/components/chat/ConversationSidebar.vue'
import MessageBubble from '@/components/chat/MessageBubble.vue'
import InkButton from '@/components/ui/InkButton.vue'
import InkSpinner from '@/components/ui/InkSpinner.vue'
import { useChatStream } from '@/composables/useChatStream'
import { useToast } from '@/composables/useToast'
import { useChatStore } from '@/stores/chat'
import type { CitationItem } from '@/utils/citations'
import { errorMessage } from '@/utils/errors'

const props = defineProps<{ conversationId?: string }>()

const store = useChatStore()
const stream = useChatStream()
const toast = useToast()
const router = useRouter()

const scroller = ref<HTMLElement | null>(null)
/**
 * 被點到的引用**連同它屬於哪一則**。只記編號的話，點較早那則的 3 會去 highlight
 * 面板當下顯示的第 3 筆，而面板顯示的是另一則的來源——兩個 3 毫無關係。
 */
const activeCitation = ref<{ messageId: string; index: number } | null>(null)
let selfNavigated = false

/** 這條 buffer 是不是「畫面上這個對話」的。不是的話，本頁一個字都不該顯示它。 */
const streamingHere = computed(() =>
  store.streaming !== null && store.streaming.conversationId === props.conversationId
    ? store.streaming
    : null,
)

/**
 * 輸入框反映的是**這一頁**的生成狀態：A 在生成不該把 B 的輸入框鎖起來。
 * 「算不算生成中」（`stopping` 也算）只在 store 定義一次，這裡只加上頁面這個條件。
 */
const isGeneratingHere = computed(() => streamingHere.value !== null && store.isGenerating)

const conversation = computed(
  () => store.conversations.find((item) => item.id === props.conversationId) ?? null,
)
const title = computed(() => {
  if (props.conversationId === undefined) {
    return '新對話'
  }
  const name = conversation.value?.title ?? ''
  return name === '' ? '對話' : name
})

/**
 * 面板顯示哪一則的來源：**點過的那一則優先**，其次是正在生成的，最後才是最後一則
 * 有引用的回答。每則訊息各有各的來源，而畫面上只有一個面板——點誰就看誰。
 */
const citationSource = computed<{ messageId: string; items: CitationItem[] } | null>(() => {
  const picked = activeCitation.value
  const buffer = streamingHere.value
  if (picked !== null) {
    if (buffer !== null && buffer.messageId === picked.messageId) {
      return { messageId: picked.messageId, items: buffer.citations }
    }
    const message = store.messages.find((item) => item.id === picked.messageId)
    if (message !== undefined) {
      return { messageId: picked.messageId, items: (message.citations ?? []) as CitationItem[] }
    }
  }
  if (buffer !== null) {
    return { messageId: buffer.messageId, items: buffer.citations }
  }
  const last = [...store.messages]
    .reverse()
    .find((item) => Array.isArray(item.citations) && item.citations.length > 0)
  return last === undefined
    ? null
    : { messageId: last.id, items: (last.citations ?? []) as CitationItem[] }
})

const citations = computed<CitationItem[]>(() => citationSource.value?.items ?? [])

/** 只有面板正在顯示那一則時，highlight 才有意義。 */
const activeIndex = computed<number | null>(() => {
  const picked = activeCitation.value
  return picked !== null && picked.messageId === citationSource.value?.messageId
    ? picked.index
    : null
})

// 新的一則開始生成 = 使用者的注意力已經移到它身上，面板跟著走（別再釘在舊的那則）。
watch(
  () => store.streaming?.messageId,
  () => {
    activeCitation.value = null
  },
)

watch(
  () => props.conversationId,
  async (conversationId) => {
    if (selfNavigated) {
      selfNavigated = false
      return
    }
    activeCitation.value = null
    if (conversationId === undefined) {
      // 回到「新對話」：清空畫面，但不動 streaming——上一場的生成在背景收尾。
      store.currentConversationId = null
      store.messages = []
      // 首頁輸入列帶來的草稿：取走即送。先取後清，send 失敗也不重複送。
      const draft = store.pendingDraft
      if (draft !== null) {
        store.pendingDraft = null
        void send(draft)
      }
      return
    }
    try {
      await store.fetchMessages(conversationId)
      await resumeUnfinished(conversationId)
      await scrollToBottom()
    } catch (error) {
      toast.error(errorMessage(error))
    }
  },
  { immediate: true },
)

// 字一直長出來的時候要跟著捲。watch 而不是在每個事件裡呼叫：來源有三個
// （送出、串流、重新載入），逐一接線遲早會漏掉一個。
watch(
  () => [store.messages.length, streamingHere.value?.text] as const,
  () => {
    void scrollToBottom()
  },
)

async function scrollToBottom(): Promise<void> {
  await nextTick()
  const element = scroller.value
  if (element !== null) {
    element.scrollTop = element.scrollHeight
  }
}

/** 重新整理之後把還在生成的那則接回去（`Last-Event-ID` 不帶＝從頭，字才不會缺）。 */
async function resumeUnfinished(conversationId: string): Promise<void> {
  const pending = store.messages.find(
    (item) => item.role === 'assistant' && item.status === 'streaming',
  )
  if (pending === undefined) {
    return
  }
  store.beginStreaming({ messageId: pending.id, conversationId })
  await stream.start({ conversationId, messageId: pending.id })
}

async function send(content: string): Promise<void> {
  try {
    let conversationId = props.conversationId
    if (conversationId === undefined) {
      // 標題＝第一句的前 24 字。09 §2.4 的「以第一句命名」後端尚未實作（已記回報
      // 清單）；建立時就帶上，側欄不會堆一排（未命名），後端補上時拿掉這行即可。
      const title = content.replace(/\s+/g, ' ').trim().slice(0, 24)
      const created = await store.createConversation({ kb_ids: [], title })
      conversationId = created.id
      selfNavigated = true
      // watch 被 selfNavigated 跳過，所以「畫面上是哪個對話」得自己說一次——
      // `finishStreaming` 靠它判斷該不該重抓，漏了的話回答生成完卻不會出現。
      store.currentConversationId = conversationId
      await router.push({ name: 'chat-conversation', params: { conversationId } })
    }
    const turn = await store.sendMessage(conversationId, content)
    await scrollToBottom()
    await stream.start({ conversationId, messageId: turn.message_id })
  } catch (error) {
    toast.error(errorMessage(error))
  }
}

async function stop(): Promise<void> {
  if (streamingHere.value === null) {
    return // 停止鈕只停這一頁的那條
  }
  try {
    await store.stopStreaming()
  } catch (error) {
    toast.error(errorMessage(error))
  }
}

/** 重試＝把上一句再問一次。失敗的那一則留在畫面上（它的字是有效內容）。 */
async function retry(): Promise<void> {
  const lastUserMessage = [...store.messages].reverse().find((item) => item.role === 'user')
  if (lastUserMessage === undefined) {
    return
  }
  await send(lastUserMessage.content)
}

function openConversation(conversationId: string): void {
  void router.push({ name: 'chat-conversation', params: { conversationId } })
}

function startNew(): void {
  void router.push({ name: 'chat' })
}

function onDeleted(conversationId: string): void {
  if (conversationId === props.conversationId) {
    void router.push({ name: 'chat' })
  }
}
</script>

<template>
  <div class="chat ink-appear">
    <ConversationSidebar
      :active-id="props.conversationId ?? null"
      @select="openConversation"
      @create="startNew"
      @deleted="onDeleted"
    />

    <section class="thread">
      <header class="head col">
        <h1 class="thread-title">{{ title }}</h1>
      </header>

      <div ref="scroller" class="scroller">
        <div class="scroller-inner col">
          <InkSpinner v-if="store.loadingMessages" text="載入中……" />

          <!-- 空對話不能是一片虛空：視線得有地方落。題句居中，落在輸入框正上方。 -->
          <div
            v-else-if="store.messages.length === 0 && streamingHere === null"
            class="empty-state"
          >
            <p class="empty-greeting">以文會友，答疑解惑</p>
            <p class="empty-hint">問一句吧——回答引用知識庫時，來源會攤在右側的箋紙上。</p>
          </div>

          <MessageBubble
            v-for="item in store.messages"
            :key="item.id"
            :role="item.role"
            :content="item.content"
            :citations="(item.citations as CitationItem[]) ?? []"
            @citation-click="activeCitation = { messageId: item.id, index: $event }"
          />

          <MessageBubble
            v-if="streamingHere !== null"
            role="assistant"
            :content="streamingHere.text"
            :citations="streamingHere.citations"
            :streaming="streamingHere.status !== 'error'"
            @citation-click="activeCitation = { messageId: streamingHere.messageId, index: $event }"
          />

          <div v-if="streamingHere?.status === 'error'" class="failure" role="alert">
            <span class="failure-title">{{ streamingHere.error?.title ?? '生成失敗' }}</span>
            <InkButton v-if="streamingHere.error?.retryable" size="small" @click="retry">重試</InkButton>
          </div>
        </div>
      </div>

      <ChatComposer class="col" :generating="isGeneratingHere" @send="send" @stop="stop" />
    </section>

    <!-- 有引用才攤開箋紙：氣泡自帶「未引用」提示，空面板只是一塊漂浮物。 -->
    <Transition name="panel">
      <CitationPanel
        v-if="citations.length > 0"
        :citations="citations"
        :active-index="activeIndex"
      />
    </Transition>
  </div>
</template>

<style scoped>
/* 佈局是 route meta.bare（無 content padding）：側欄要貼齊左緣、撐滿高度。 */
.chat {
  display: flex;
  align-items: stretch;
  flex: 1;
  min-height: 0;
}

.thread {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 22px 36px 28px;
  box-sizing: border-box;
}

/* 單一閱讀軸：標題、訊息、輸入框全落在同一條 50rem 的欄上 */
.col {
  width: 100%;
  max-width: 50rem;
  margin-inline: auto;
  box-sizing: border-box;
}

.thread-title {
  margin: 0;
  font-family: var(--font-serif);
  font-size: 1.0625rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--ink-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* flex: 1 而非固定高：訊息區吃掉剩餘高度，輸入框自然釘底 */
.scroller {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding-right: 8px;
}

/* min-height: 100% 讓空狀態能用 margin: auto 垂直置中 */
.scroller-inner {
  min-height: 100%;
  display: flex;
  flex-direction: column;
  gap: 16px;
  box-sizing: border-box;
}

.empty-state {
  margin: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  padding: 24px;
  text-align: center;
}

.empty-greeting {
  margin: 0;
  font-family: var(--font-kai);
  font-size: 1.375rem;
  letter-spacing: 0.3em;
  text-indent: 0.3em; /* 抵銷末字後的字距，視覺才真正置中 */
  color: var(--ink-3);
}

.empty-hint {
  margin: 0;
  font-size: 0.8125rem;
  color: var(--ink-4);
}

.panel-enter-active,
.panel-leave-active {
  transition:
    opacity var(--dur-slow) var(--ease-ink),
    transform var(--dur-slow) var(--ease-ink);
}

.panel-enter-from,
.panel-leave-to {
  opacity: 0;
  transform: translateX(14px);
}

/* 箋紙不貼畫面右緣（thread 有自己的 padding，面板是獨立欄） */
.chat > :deep(.panel) {
  margin: 22px 24px 28px 0;
}

@media (max-width: 1024px) {
  .chat > :deep(.panel) {
    display: none; /* 窄幅先讓位給對話；引用記號仍在氣泡內。2D 跳原文時再一併設計 */
  }
}

@media (max-width: 900px) {
  .thread {
    padding: 16px 16px 20px;
  }
}

.failure {
  align-self: flex-start;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  border: 1px solid color-mix(in srgb, var(--cinnabar) 50%, transparent);
  border-radius: var(--radius-a);
  background: color-mix(in srgb, var(--cinnabar) 6%, transparent);
}

.failure-title {
  font-size: 0.875rem;
  color: var(--cinnabar);
}
</style>

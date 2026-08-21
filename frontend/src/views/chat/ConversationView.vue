<script setup lang="ts">
/**
 * 一場對話（1E-3；09 §2.4、§3.2、03 §3.2）。
 *
 * 送出與串流是**兩步**（1D-4a）：`sendMessage()` 建立回合並拿到 `message_id`，
 * 再用它開串流。合成一步的話，網路閃斷時使用者分不出送出去了沒，重送就是兩則
 * 訊息、兩次生成、兩次帳單。
 *
 * 重新整理（或深層連結）進來時，**沒讀完的那則會自己接回去**：訊息清單裡若有一則
 * 還在 `streaming`，就對它開一條串流——後端的生成不因為 client 離開而停止（06 §4
 * 的 G-06），所以接回去看到的是完整的後半段。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import ChatComposer from '@/components/chat/ChatComposer.vue'
import CitationPanel from '@/components/chat/CitationPanel.vue'
import MessageBubble from '@/components/chat/MessageBubble.vue'
import BrushDivider from '@/components/ui/BrushDivider.vue'
import InkButton from '@/components/ui/InkButton.vue'
import InkSpinner from '@/components/ui/InkSpinner.vue'
import { useChatStream } from '@/composables/useChatStream'
import { useToast } from '@/composables/useToast'
import { useChatStore } from '@/stores/chat'
import type { CitationItem } from '@/utils/citations'
import { errorMessage } from '@/utils/errors'

const props = defineProps<{ conversationId: string }>()

const store = useChatStore()
const stream = useChatStream()
const toast = useToast()

const scroller = ref<HTMLElement | null>(null)
const activeCitation = ref<number | null>(null)

const conversation = computed(
  () => store.conversations.find((item) => item.id === props.conversationId) ?? null,
)
const title = computed(() => {
  const name = conversation.value?.title ?? ''
  return name === '' ? '對話' : name
})

/**
 * 面板顯示哪一組來源：正在生成時看 buffer，否則看最後一則有引用的回答。
 * 每則訊息各有各的來源，而畫面上只有一個面板——以「使用者現在在看的那一則」為準。
 */
const citations = computed<CitationItem[]>(() => {
  if (store.streaming !== null) {
    return store.streaming.citations
  }
  const lastWithCitations = [...store.messages]
    .reverse()
    .find((item) => Array.isArray(item.citations) && item.citations.length > 0)
  return (lastWithCitations?.citations ?? []) as CitationItem[]
})

onMounted(() => {
  if (store.conversations.length === 0) {
    // 深層連結進來時清單還沒載——標題要用它。失敗不打擾：主體是訊息，不是標題。
    void store.fetchConversations().catch(() => {})
  }
})

watch(
  () => props.conversationId,
  async (conversationId) => {
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
  () => [store.messages.length, store.streaming?.text] as const,
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
    const turn = await store.sendMessage(props.conversationId, content)
    await scrollToBottom()
    await stream.start({ conversationId: props.conversationId, messageId: turn.message_id })
  } catch (error) {
    toast.error(errorMessage(error))
  }
}

async function stop(): Promise<void> {
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
</script>

<template>
  <div class="conversation ink-appear">
    <section class="thread">
      <header class="head col">
        <h1 class="page-title">{{ title }}</h1>
        <BrushDivider class="head-divider" />
      </header>

      <div ref="scroller" class="scroller">
        <div class="scroller-inner col">
          <InkSpinner v-if="store.loadingMessages" text="載入中……" />

          <!-- 空對話不能是一片虛空：視線得有地方落。題句居中，落在輸入框正上方。 -->
          <div
            v-else-if="store.messages.length === 0 && store.streaming === null"
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
            @citation-click="activeCitation = $event"
          />

          <MessageBubble
            v-if="store.streaming !== null"
            role="assistant"
            :content="store.streaming.text"
            :citations="store.streaming.citations"
            :streaming="store.streaming.status !== 'error'"
            @citation-click="activeCitation = $event"
          />

          <div v-if="store.streaming?.status === 'error'" class="failure" role="alert">
            <span class="failure-title">{{ store.streaming.error?.title ?? '生成失敗' }}</span>
            <InkButton v-if="store.streaming.error?.retryable" size="small" @click="retry">重試</InkButton>
          </div>
        </div>
      </div>

      <ChatComposer class="col" :generating="store.isGenerating" @send="send" @stop="stop" />
    </section>

    <!-- 有引用才攤開箋紙：氣泡自帶「未引用」提示，空面板只是第三塊漂浮物。 -->
    <Transition name="panel">
      <CitationPanel
        v-if="citations.length > 0"
        :citations="citations"
        :active-index="activeCitation"
      />
    </Transition>
  </div>
</template>

<style scoped>
/* stretch 而非 flex-start：箋紙與對話欄同高，才是一幅畫而不是兩張卡。 */
.conversation {
  display: flex;
  gap: 30px;
  align-items: stretch;
  flex: 1;
  min-height: 0;
}

.thread {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

/* 單一閱讀軸：標題、訊息、輸入框全落在同一條 50rem 的欄上——
   三塊各對各的邊，就是畫面「散成三塊」的根源。 */
.col {
  width: 100%;
  max-width: 50rem;
  margin-inline: auto;
  box-sizing: border-box;
}

.head {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.page-title {
  margin: 0;
  font-size: 1.5rem;
}

.head-divider {
  width: 210px;
}

/* flex: 1 取代寫死的 58vh：訊息區吃掉剩餘高度，輸入框自然釘底。 */
.scroller {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding-right: 8px;
}

/* min-height: 100% 讓空狀態能用 margin: auto 垂直置中。 */
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

@media (max-width: 1024px) {
  .conversation {
    flex-direction: column;
    gap: 20px;
  }

  /* 窄幅時箋紙移到下方，高度封頂、自己捲，不把輸入框擠出畫面 */
  .conversation > :deep(.panel) {
    width: 100%;
    max-height: 36vh;
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

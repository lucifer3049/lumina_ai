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
import { NButton, NCard, NResult, NSpace, NSpin, useMessage } from 'naive-ui'
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import ChatComposer from '@/components/chat/ChatComposer.vue'
import CitationPanel from '@/components/chat/CitationPanel.vue'
import MessageBubble from '@/components/chat/MessageBubble.vue'
import { useChatStream } from '@/composables/useChatStream'
import { useChatStore } from '@/stores/chat'
import type { CitationItem } from '@/utils/citations'
import { errorMessage } from '@/utils/errors'

const props = defineProps<{ conversationId: string }>()

const store = useChatStore()
const stream = useChatStream()
const message = useMessage()

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
      message.error(errorMessage(error))
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
    message.error(errorMessage(error))
  }
}

async function stop(): Promise<void> {
  try {
    await store.stopStreaming()
  } catch (error) {
    message.error(errorMessage(error))
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
  <div class="conversation">
    <NCard :title="title" class="thread">
      <div ref="scroller" class="scroller">
        <NSpin v-if="store.loadingMessages" size="small" />

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

        <NResult
          v-if="store.streaming?.status === 'error'"
          status="warning"
          :title="store.streaming.error?.title ?? '生成失敗'"
          size="small"
          class="failure"
        >
          <template #footer>
            <NSpace>
              <NButton v-if="store.streaming.error?.retryable" size="small" @click="retry">
                重試
              </NButton>
            </NSpace>
          </template>
        </NResult>
      </div>

      <ChatComposer :generating="store.isGenerating" @send="send" @stop="stop" />
    </NCard>

    <CitationPanel :citations="citations" :active-index="activeCitation" />
  </div>
</template>

<style scoped>
.conversation {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}

.thread {
  flex: 1;
  min-width: 0;
}

.scroller {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 60vh;
  overflow-y: auto;
  margin-bottom: 16px;
  padding-right: 8px;
}

.failure {
  align-self: flex-start;
}
</style>

<script setup lang="ts">
/**
 * 一則訊息（1E-3；03 §2 components/chat）。
 *
 * 引用的渲染走 `renderAnswer()` 切好的 segments，**不用 `v-html`**（CLAUDE.md 鐵則
 * 10）：內容是模型產生的字串，把它當 HTML 塞進 DOM 等於讓模型（或它讀到的文件）
 * 決定頁面上會執行什麼。
 *
 * 對不上來源的 `[c:n]` 在 `renderAnswer()` 就消失了（13 §3 缺口①）——這裡不需要
 * 再判斷一次，也不該：兩個地方各判一次，遲早會有一邊漏掉。
 *
 * 視覺：使用者＝濃墨落紙，助理＝紙上墨字；引用編號是小朱印（設計語言）。
 */
import { computed } from 'vue'

import { renderAnswer, type CitationItem } from '@/utils/citations'

const props = defineProps<{
  role: string
  content: string
  citations?: readonly CitationItem[]
  /** 生成中：句尾要有游標，且不顯示「無引用」之類的收尾提示。 */
  streaming?: boolean
}>()

const emit = defineEmits<{ (event: 'citationClick', index: number): void }>()

const isUser = computed(() => props.role === 'user')
const segments = computed(() => renderAnswer(props.content, props.citations ?? []))
</script>

<template>
  <div class="bubble" :class="isUser ? 'bubble--user' : 'bubble--assistant'">
    <div class="body">
      <template v-for="(segment, index) in segments" :key="index">
        <span v-if="segment.kind === 'text'" class="text">{{ segment.text }}</span>
        <button
          v-else
          type="button"
          class="citation"
          :title="segment.docName"
          @click="emit('citationClick', segment.index)"
        >
          {{ segment.index }}
        </button>
      </template>
      <!-- 游標畫在文字流的最後，跟著字一起長——固定在角落的話，長回答會看不到它。 -->
      <span v-if="props.streaming" class="cursor" aria-hidden="true">▍</span>
    </div>
    <span v-if="!isUser && !props.streaming && (props.citations?.length ?? 0) === 0" class="hint">
      本回答未引用知識庫內容
    </span>
  </div>
</template>

<style scoped>
.bubble {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-width: 46rem;
  padding: 12px 17px;
  line-height: 1.9;
  font-size: 0.875rem;
}

/* 圓潤氣泡（GPT 式）：說話那一側的下角收小，當作朝向說話者的筆勢 */
.bubble--user {
  align-self: flex-end;
  background: var(--accent-deep);
  color: var(--accent-deep-ink);
  border-radius: 18px 18px 6px 18px;
  box-shadow: var(--shadow-ink);
}

.bubble--assistant {
  align-self: flex-start;
  background: color-mix(in srgb, var(--paper-2) 80%, transparent);
  border: 1px solid var(--paper-5);
  border-radius: 18px 18px 18px 6px;
}

.body {
  white-space: pre-wrap;
  word-break: break-word;
}

/* 引用編號＝小朱印 */
.citation {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 17px;
  height: 17px;
  margin: 0 3px;
  padding: 0 3px;
  vertical-align: 2px;
  border: 1px solid color-mix(in srgb, var(--cinnabar) 50%, transparent);
  border-radius: 6px;
  background: color-mix(in srgb, var(--cinnabar) 8%, transparent);
  color: var(--cinnabar);
  font-size: 0.6875rem;
  line-height: 1;
  cursor: pointer;
  transition: background-color var(--dur-fast) var(--ease-ink);
}

.citation:hover {
  background: color-mix(in srgb, var(--cinnabar) 16%, transparent);
}

.cursor {
  animation: blink 1s step-end infinite;
}

.hint {
  font-size: 0.8125rem;
  color: var(--ink-4);
}

@keyframes blink {
  50% {
    opacity: 0;
  }
}
</style>

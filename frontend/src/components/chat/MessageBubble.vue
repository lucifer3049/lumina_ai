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
 */
import { NTag, NText } from 'naive-ui'
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
        <NTag
          v-else
          size="small"
          round
          class="citation"
          :title="segment.docName"
          @click="emit('citationClick', segment.index)"
        >
          {{ segment.index }}
        </NTag>
      </template>
      <!-- 游標畫在文字流的最後，跟著字一起長——固定在角落的話，長回答會看不到它。 -->
      <span v-if="props.streaming" class="cursor" aria-hidden="true">▍</span>
    </div>
    <NText
      v-if="!isUser && !props.streaming && (props.citations?.length ?? 0) === 0"
      depth="3"
      class="hint"
    >
      本回答未引用知識庫內容
    </NText>
  </div>
</template>

<style scoped>
.bubble {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-width: 46rem;
  padding: 10px 14px;
  border-radius: 10px;
  line-height: 1.7;
}

.bubble--user {
  align-self: flex-end;
  background: var(--n-color-embedded, rgba(128, 128, 128, 0.12));
}

.bubble--assistant {
  align-self: flex-start;
}

.body {
  white-space: pre-wrap;
  word-break: break-word;
}

.citation {
  margin: 0 2px;
  cursor: pointer;
  vertical-align: super;
  font-size: 0.72rem;
}

.cursor {
  animation: blink 1s step-end infinite;
}

.hint {
  font-size: 0.8125rem;
}

@keyframes blink {
  50% {
    opacity: 0;
  }
}
</style>

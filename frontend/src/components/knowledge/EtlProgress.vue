<script setup lang="ts">
/**
 * 一份文件的 ETL 進度（1E-2；08 §2、§6）。
 *
 * 進度來自狀態機而不是百分比：後端沒有回進度數字（`DocumentOut` 只有 `status`），
 * 而頁級百分比要等 etl_jobs 對外（08 §5）。用階段序位畫進度條至少讓人看得出
 * 「還在動」與「卡住了」的差別。
 *
 * 失敗時進度條讓位給原因：`error.message` 是後端已經過濾過的對外訊息
 * （`services/knowledge/failures.py`——第三方例外的字串一律換成固定句子，
 * 避免夾帶 bucket 名、SQL 片段或 API key 前綴），可以直接顯示給租戶看。
 * 階段/例外型別的除錯細節放 title（hover 可見），不佔版面。
 */
import { computed } from 'vue'

import InkProgress from '@/components/ui/InkProgress.vue'
import InkTag from '@/components/ui/InkTag.vue'
import {
  documentProgressPercent,
  documentStatusLabel,
  isDocumentFailed,
} from '@/utils/documentStatus'

const props = defineProps<{
  status: string
  /** 結構化失敗原因：`{ stage, cause, message }`。null = 沒失敗過。 */
  error?: Record<string, unknown> | null
}>()

const failed = computed(() => isDocumentFailed(props.status))
const settled = computed(() => props.status === 'ready')
const percent = computed(() => documentProgressPercent(props.status))
const label = computed(() => documentStatusLabel(props.status))

const asText = (value: unknown): string => (typeof value === 'string' ? value : '')

/** 給人看的那一句。後端沒給訊息時退回狀態標籤，不留空白。 */
const failureMessage = computed(() => asText(props.error?.message) || label.value)
/** 給查問題的人看的：哪一階段、什麼例外型別。 */
const failureDetail = computed(() => {
  const stage = asText(props.error?.stage)
  const cause = asText(props.error?.cause)
  return [stage && `階段：${stage}`, cause && `類型：${cause}`].filter(Boolean).join('　')
})
</script>

<template>
  <div class="etl-progress">
    <template v-if="failed">
      <span :title="failureDetail || undefined">
        <InkTag tone="error">{{ label }}</InkTag>
      </span>
      <span class="message">{{ failureMessage }}</span>
    </template>

    <template v-else-if="settled">
      <InkTag tone="success">
        <svg
          width="11"
          height="11"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2.4"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <path d="M4 12.5 L 9.5 18 L 20 6.5"></path>
        </svg>
        {{ label }}
      </InkTag>
    </template>

    <template v-else>
      <InkTag tone="info">{{ label }}</InkTag>
      <!-- 進度條只在跑的時候出現：終點狀態再放一條 100% 的線只是噪音。 -->
      <InkProgress :percent="percent" class="bar" />
    </template>
  </div>
</template>

<style scoped>
.etl-progress {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 12rem;
}

.bar {
  flex: 1;
  min-width: 4rem;
}

.message {
  font-size: 0.8125rem;
  color: var(--ink-4);
}
</style>

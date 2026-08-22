<script setup lang="ts">
/**
 * 輸入框（1E-3）。
 *
 * Enter 送出、Shift+Enter 換行——這是聊天介面的通用預期，而 LLM 的問題常常是多行。
 * 生成中不擋輸入（可以先打下一句），但送出鈕換成「停止」：那是使用者在等待期間
 * 唯一想按的東西。送出鈕是墨圓（視覺稿定案）。
 */
import { ref } from 'vue'

import InkInput from '@/components/ui/InkInput.vue'

const props = defineProps<{ generating: boolean; disabled?: boolean }>()
const emit = defineEmits<{ (event: 'send', content: string): void; (event: 'stop'): void }>()

const draft = ref('')

function send(): void {
  const content = draft.value.trim()
  if (content === '' || props.generating || props.disabled === true) {
    return
  }
  draft.value = ''
  emit('send', content)
}
</script>

<template>
  <div class="composer">
    <InkInput
      v-model="draft"
      type="textarea"
      :rows="2"
      class="input"
      placeholder="問一個問題（Enter 送出，Shift+Enter 換行）"
      :disabled="props.disabled"
      @keydown.enter.exact.prevent="send"
    />
    <button
      v-if="props.generating"
      type="button"
      class="round-button stop"
      aria-label="停止生成"
      @click="emit('stop')"
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <rect x="6" y="6" width="12" height="12" rx="1"></rect>
      </svg>
    </button>
    <button
      v-else
      type="button"
      class="round-button send"
      aria-label="送出"
      :disabled="props.disabled"
      @click="send"
    >
      <svg
        width="17"
        height="17"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d="M4 12 L20 4 L14 20 L11 13 Z"></path>
      </svg>
    </button>
  </div>
</template>

<style scoped>
.composer {
  display: flex;
  gap: 14px;
  align-items: flex-end;
}

.input {
  flex: 1;
  min-width: 0;
}

/* 聊天輸入框比一般輸入框更圓（GPT 式膠囊感），padding 跟著放大才不擠。
   雙類名選擇器：要壓過 InkInput 自己的 .ink-input 規則。 */
.composer .input {
  border-radius: var(--radius-c);
  padding: 13px 18px;
}

/* 墨圓：不規則圓角＋微旋，像一滴按下去的墨 */
.round-button {
  flex-shrink: 0;
  width: 48px;
  height: 48px;
  border: none;
  border-radius: 48% 52% 50% 46%;
  transform: rotate(-2deg);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  box-shadow: var(--shadow-ink);
  transition:
    opacity var(--dur-med) var(--ease-ink),
    box-shadow var(--dur-med) var(--ease-ink);
}

.round-button svg {
  transform: rotate(2deg);
}

.send {
  background: var(--accent-deep);
  color: var(--accent-deep-ink);
}

.send:hover:not(:disabled) {
  opacity: 0.9;
}

.send:disabled {
  opacity: 0.45;
  cursor: default;
}

.stop {
  background: transparent;
  border: 1px solid var(--ink-4);
  color: var(--ink-2);
  box-shadow: none;
}

.stop:hover {
  background: var(--paper-3);
}
</style>

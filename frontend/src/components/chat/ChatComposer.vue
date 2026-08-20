<script setup lang="ts">
/**
 * 輸入框（1E-3）。
 *
 * Enter 送出、Shift+Enter 換行——這是聊天介面的通用預期，而 LLM 的問題常常是多行。
 * 生成中不擋輸入（可以先打下一句），但送出鈕換成「停止」：那是使用者在等待期間
 * 唯一想按的東西。
 */
import { NButton, NInput, NSpace } from 'naive-ui'
import { ref } from 'vue'

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
    <NInput
      v-model:value="draft"
      type="textarea"
      :autosize="{ minRows: 2, maxRows: 8 }"
      placeholder="問一個問題（Enter 送出，Shift+Enter 換行）"
      :disabled="props.disabled"
      @keydown.enter.exact.prevent="send"
    />
    <NSpace justify="end" class="actions">
      <NButton v-if="props.generating" secondary @click="emit('stop')">停止生成</NButton>
      <NButton v-else type="primary" :disabled="props.disabled" @click="send">送出</NButton>
    </NSpace>
  </div>
</template>

<style scoped>
.composer {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.actions {
  width: 100%;
}
</style>

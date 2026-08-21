<script setup lang="ts">
/**
 * 上傳區（1E-2；03 §2 components/knowledge）。
 *
 * 元件本身不知道怎麼上傳——實際動作由 `upload` prop 注入（view 呼叫 store）。
 * 改為原生 drag & drop + 隱藏 file input（1E 附錄：換掉 naive-ui 之後，
 * 「等 promise 才知道成敗」的契約直接由 await 表達，失敗的 toast 由 view 發）。
 *
 * `accept` 只是選擇器的提示——後端以內容（magic bytes）判定型別，副檔名不參與
 * （`services/knowledge/uploads.py`）。
 */
import { ref } from 'vue'

import { ACCEPTED_UPLOAD_EXTENSIONS, MAX_UPLOAD_BYTES } from '@/services/uploadService'

const props = defineProps<{
  /** 實際上傳一個檔案；失敗會 reject——這裡吞掉（訊息由 view 顯示），逐檔繼續。 */
  upload: (file: File) => Promise<void>
  disabled?: boolean
}>()

const sizeLimitText = `${MAX_UPLOAD_BYTES / 1024 / 1024}MB`

const fileInput = ref<HTMLInputElement | null>(null)
const dragOver = ref(false)
const uploadingCount = ref(0)

function openPicker(): void {
  if (props.disabled !== true) {
    fileInput.value?.click()
  }
}

function onPick(event: Event): void {
  const input = event.target as HTMLInputElement
  void handleFiles(input.files)
  // 清掉才能重選同一個檔案（change 事件以值變化為準）。
  input.value = ''
}

function onDrop(event: DragEvent): void {
  dragOver.value = false
  if (props.disabled === true) {
    return
  }
  void handleFiles(event.dataTransfer?.files ?? null)
}

async function handleFiles(files: FileList | null): Promise<void> {
  if (files === null || files.length === 0) {
    return
  }
  for (const file of Array.from(files)) {
    uploadingCount.value += 1
    try {
      await props.upload(file)
    } catch {
      // view 已 toast 過；這裡吞掉讓後面的檔案繼續傳。
    } finally {
      uploadingCount.value -= 1
    }
  }
}
</script>

<template>
  <!-- input 放在 button 外面：程式觸發的 input.click() 若在 button 內，
       click 會冒泡回 button 再開一次選擇器（遞迴）。 -->
  <input
    ref="fileInput"
    type="file"
    class="hidden-input"
    multiple
    :accept="ACCEPTED_UPLOAD_EXTENSIONS"
    @change="onPick"
  />
  <button
    type="button"
    class="dropzone"
    :class="{ over: dragOver, disabled: props.disabled }"
    :disabled="props.disabled"
    @click="openPicker"
    @dragover.prevent="dragOver = true"
    @dragleave="dragOver = false"
    @drop.prevent="onDrop"
  >
    <svg
      width="26"
      height="26"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.5"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M12 16 L12 5"></path>
      <path d="M7 9.5 L12 4.5 L17 9.5"></path>
      <path
        d="M4.5 16.5 L4.5 18.5 C4.5 19.05 4.95 19.5 5.5 19.5 L18.5 19.5 C19.05 19.5 19.5 19.05 19.5 18.5 L19.5 16.5"
      ></path>
    </svg>
    <span class="main-text">
      {{ uploadingCount > 0 ? `上傳中（${uploadingCount} 份）……` : '將檔案拖放至此處，或點擊選擇檔案' }}
    </span>
    <span class="sub-text">支援 PDF · Word · Excel · 純文字與 Markdown，單檔上限 {{ sizeLimitText }}</span>
  </button>
</template>

<style scoped>
.dropzone {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 30px;
  box-sizing: border-box;
  background: color-mix(in srgb, var(--paper-3) 55%, transparent);
  border: 1px solid var(--ink-5);
  border-radius: var(--radius-c);
  color: var(--ink-3);
  font-family: var(--font-body);
  cursor: pointer;
  transition:
    background-color var(--dur-med) var(--ease-ink),
    border-color var(--dur-med) var(--ease-ink);
}

.dropzone:hover:not(.disabled),
.dropzone.over {
  background: color-mix(in srgb, var(--paper-3) 85%, transparent);
  border-color: var(--ink-4);
}

.dropzone.disabled {
  cursor: default;
  opacity: 0.6;
}

.hidden-input {
  display: none;
}

.main-text {
  font-size: 0.875rem;
  letter-spacing: 0.06em;
  color: var(--ink-2);
}

.sub-text {
  font-size: 0.75rem;
  letter-spacing: 0.04em;
  color: var(--ink-4);
}
</style>

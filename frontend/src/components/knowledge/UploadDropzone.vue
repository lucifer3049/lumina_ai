<script setup lang="ts">
/**
 * 上傳區（1E-2；03 §2 components/knowledge）。
 *
 * 元件本身不知道怎麼上傳——實際動作由 `upload` prop 注入（view 呼叫 store）。
 * 用 prop 而不是 emit 是因為 naive-ui 的 `custom-request` 要等一個 promise 才知道
 * 該把那一列標成成功或失敗，而 emit 沒有回傳值：改用 emit 的話，每個檔案都會
 * 立刻顯示成功，包含被後端擋下來的那些。
 *
 * `accept` 只是選擇器的提示——後端以內容（magic bytes）判定型別，副檔名不參與
 * （`services/knowledge/uploads.py`）。
 */
import { NUpload, NUploadDragger, NText } from 'naive-ui'
import type { UploadCustomRequestOptions } from 'naive-ui'

import { ACCEPTED_UPLOAD_EXTENSIONS, MAX_UPLOAD_BYTES } from '@/services/uploadService'

const props = defineProps<{
  /** 實際上傳一個檔案；失敗要 reject，這一列才會標成失敗。 */
  upload: (file: File) => Promise<void>
  disabled?: boolean
}>()

const sizeLimitText = `${MAX_UPLOAD_BYTES / 1024 / 1024}MB`

async function customRequest({
  file,
  onFinish,
  onError,
}: UploadCustomRequestOptions): Promise<void> {
  if (file.file == null) {
    onError()
    return
  }
  try {
    await props.upload(file.file)
    onFinish()
  } catch {
    // 錯誤訊息由 view 顯示（它才知道要用哪個 message provider）；
    // 這裡只負責讓該列變成失敗狀態。
    onError()
  }
}
</script>

<template>
  <NUpload
    multiple
    directory-dnd
    :accept="ACCEPTED_UPLOAD_EXTENSIONS"
    :custom-request="customRequest"
    :disabled="props.disabled"
    :show-file-list="false"
  >
    <NUploadDragger>
      <NText style="font-size: 1rem">把檔案拖到這裡，或點擊選擇</NText>
      <br />
      <NText depth="3">
        支援 PDF、Word、Excel、純文字與 Markdown，單檔上限 {{ sizeLimitText }}
      </NText>
    </NUploadDragger>
  </NUpload>
</template>

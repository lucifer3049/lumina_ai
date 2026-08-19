<script setup lang="ts">
/**
 * 一個知識庫底下的文件（1E-2；03 §2、08 §2）。
 *
 * 這一頁是 1E-2 唯一會「自己動」的畫面：ETL 在背景跑，狀態要自己更新，否則
 * 使用者上傳完只看得到「已收下」，然後盯著一個永遠不變的畫面猜有沒有在跑。
 *
 * 輪詢的開關綁在 `hasPendingDocuments`：有東西在跑才問，全部到終點就停
 * （`usePolling` 另外處理不重疊、退讓與放棄）。
 */
import {
  NButton,
  NCard,
  NDataTable,
  NEmpty,
  NPopconfirm,
  NSpace,
  NText,
  useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { computed, h, onMounted, watch } from 'vue'

import EtlProgress from '@/components/knowledge/EtlProgress.vue'
import UploadDropzone from '@/components/knowledge/UploadDropzone.vue'
import { usePolling } from '@/composables/usePolling'
import { describeUploadError } from '@/services/uploadService'
import { DOCUMENT_POLL_INTERVAL_MS, useKnowledgeStore } from '@/stores/knowledge'
import type { DocumentOut } from '@/types/models'
import { canReingest, isDocumentSettled } from '@/utils/documentStatus'
import { errorMessage } from '@/utils/errors'
import { formatBytes } from '@/utils/format'

const props = defineProps<{ kbId: string }>()

const store = useKnowledgeStore()
const message = useMessage()

const polling = usePolling(() => store.refreshDocuments(), {
  intervalMs: DOCUMENT_POLL_INTERVAL_MS,
  onGiveUp: () => {
    // 連續失敗到放棄：畫面得說出來，否則進度會就這樣靜止在半路。
    message.error('無法取得處理進度，請重新整理頁面')
  },
})

/**
 * 標題要顯示 KB 名字。深層連結（或重新整理）進來時 store 裡還沒有清單，
 * 補抓一次——這比為了一個名字另外呼叫 `GET /knowledge-bases/{id}` 少一個進入點，
 * 而且回到清單頁時本來就要有這份資料。
 */
const kb = computed(() => store.knowledgeBases.find((item) => item.id === props.kbId) ?? null)
const title = computed(() => kb.value?.name ?? '文件')

onMounted(() => {
  if (store.knowledgeBases.length === 0) {
    void store.fetchKnowledgeBases().catch(() => {
      // 名字拿不到不影響這一頁的主體（文件清單），不打擾使用者。
    })
  }
})

watch(
  () => props.kbId,
  async (kbId) => {
    try {
      await store.fetchDocuments(kbId)
    } catch (error) {
      message.error(errorMessage(error))
    }
  },
  { immediate: true },
)

// 有東西在跑才輪詢。watch 而不是在 fetch 之後手動 start：狀態改變的來源有三個
// （載入、上傳、重跑），逐一接線遲早會漏掉一個。
watch(
  () => store.hasPendingDocuments,
  (pending) => {
    if (pending) {
      polling.start()
    } else {
      polling.stop()
    }
  },
  { immediate: true },
)

async function upload(file: File): Promise<void> {
  try {
    await store.uploadDocument(props.kbId, file)
    message.success(`${file.name} 已收下，開始處理`)
  } catch (error) {
    message.error(describeUploadError(error))
    throw error // 讓上傳區把這一列標成失敗
  }
}

async function reingest(row: DocumentOut): Promise<void> {
  try {
    await store.reingestDocument(row.id)
    message.success('已重新排入處理')
  } catch (error) {
    message.error(errorMessage(error))
  }
}

async function remove(row: DocumentOut): Promise<void> {
  try {
    await store.deleteDocument(row.id)
    message.success('已刪除')
  } catch (error) {
    message.error(errorMessage(error))
  }
}

const columns = computed<DataTableColumns<DocumentOut>>(() => [
  {
    title: '檔名',
    key: 'filename',
    render: (row) =>
      h('div', [
        h('div', row.filename),
        // 版本只在重跑過之後才有意義（第 1 版是每份文件的預設）。
        row.doc_version > 1
          ? h(NText, { depth: 3, style: 'font-size: 0.8125rem' }, () => `第 ${row.doc_version} 版`)
          : null,
      ]),
  },
  {
    title: '大小',
    key: 'size_bytes',
    width: 110,
    render: (row) => formatBytes(row.size_bytes),
  },
  {
    title: '狀態',
    key: 'status',
    width: 280,
    render: (row) => h(EtlProgress, { status: row.status, error: row.error ?? null }),
  },
  {
    title: '',
    key: 'actions',
    width: 180,
    render: (row) =>
      h(NSpace, { size: 'small' }, () => [
        h(
          NButton,
          {
            size: 'small',
            quaternary: true,
            // 後端在處理中的狀態會回 409（documents.py），這裡先把按鈕關掉——
            // 兩邊的判定有時間差是正常的，真的撞上時錯誤訊息仍會顯示。
            disabled: !canReingest(row.status),
            onClick: () => void reingest(row),
          },
          () => '重新處理',
        ),
        h(
          NPopconfirm,
          { onPositiveClick: () => void remove(row) },
          {
            default: () => '刪除後，這份文件的內容與向量都會移除。確定嗎？',
            trigger: () =>
              h(NButton, { size: 'small', quaternary: true, type: 'error' }, () => '刪除'),
          },
        ),
      ]),
  },
])

const pendingCount = computed(
  () => store.documents.filter((document) => !isDocumentSettled(document.status)).length,
)
</script>

<template>
  <NCard :title="title">
    <template #header-extra>
      <NText v-if="pendingCount > 0" depth="3">處理中 {{ pendingCount }} 份</NText>
    </template>

    <UploadDropzone :upload="upload" class="dropzone" />

    <NDataTable
      :columns="columns"
      :data="store.documents"
      :loading="store.loadingDocuments"
      :row-key="(row: DocumentOut) => row.id"
      :bordered="false"
    >
      <template #empty>
        <NEmpty description="這個知識庫還沒有文件。傳一份上來，處理完就能拿來問答。" />
      </template>
    </NDataTable>
  </NCard>
</template>

<style scoped>
.dropzone {
  margin-bottom: 16px;
}
</style>

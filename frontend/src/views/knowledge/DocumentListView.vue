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
import { computed, onMounted, watch } from 'vue'

import EtlProgress from '@/components/knowledge/EtlProgress.vue'
import UploadDropzone from '@/components/knowledge/UploadDropzone.vue'
import BrushDivider from '@/components/ui/BrushDivider.vue'
import InkButton from '@/components/ui/InkButton.vue'
import InkConfirm from '@/components/ui/InkConfirm.vue'
import InkEmpty from '@/components/ui/InkEmpty.vue'
import InkSpinner from '@/components/ui/InkSpinner.vue'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import { describeUploadError } from '@/services/uploadService'
import { DOCUMENT_POLL_INTERVAL_MS, useKnowledgeStore } from '@/stores/knowledge'
import type { DocumentOut } from '@/types/models'
import { canReingest, isDocumentSettled } from '@/utils/documentStatus'
import { errorMessage } from '@/utils/errors'
import { formatBytes } from '@/utils/format'

const props = defineProps<{ kbId: string }>()

const store = useKnowledgeStore()
const toast = useToast()

const polling = usePolling(() => store.refreshDocuments(), {
  intervalMs: DOCUMENT_POLL_INTERVAL_MS,
  onGiveUp: () => {
    // 連續失敗到放棄：畫面得說出來，否則進度會就這樣靜止在半路。
    toast.error('無法取得處理進度，請重新整理頁面')
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
      toast.error(errorMessage(error))
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
    toast.success(`${file.name} 已收下，開始處理`)
  } catch (error) {
    toast.error(describeUploadError(error))
    throw error // 讓上傳區把這一列標成失敗
  }
}

async function reingest(row: DocumentOut): Promise<void> {
  try {
    await store.reingestDocument(row.id)
    toast.success('已重新排入處理')
  } catch (error) {
    toast.error(errorMessage(error))
  }
}

async function remove(row: DocumentOut): Promise<void> {
  try {
    await store.deleteDocument(row.id)
    toast.success('已刪除')
  } catch (error) {
    toast.error(errorMessage(error))
  }
}

const pendingCount = computed(
  () => store.documents.filter((document) => !isDocumentSettled(document.status)).length,
)
</script>

<template>
  <section class="view ink-appear">
    <header class="head">
      <div class="head-titles">
        <h1 class="page-title">
          {{ title }}
          <span class="title-seal" aria-hidden="true"></span>
        </h1>
        <BrushDivider class="head-divider" />
        <p class="page-subtitle">江南煙雨後，書卷皆有靈</p>
      </div>
      <span v-if="pendingCount > 0" class="pending">處理中 {{ pendingCount }} 份</span>
    </header>

    <UploadDropzone :upload="upload" />

    <InkSpinner v-if="store.loadingDocuments" />
    <InkEmpty
      v-else-if="store.documents.length === 0"
      description="這個知識庫還沒有文件。傳一份上來，處理完就能拿來問答。"
    />
    <table v-else class="ink-table">
      <thead>
        <tr>
          <th>檔案名稱</th>
          <th class="col-size">大小</th>
          <th class="col-status">狀態</th>
          <th class="col-actions"></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in store.documents" :key="row.id">
          <td>
            <div class="name-cell">
              <span>{{ row.filename }}</span>
              <!-- 版本只在重跑過之後才有意義（第 1 版是每份文件的預設）。 -->
              <span v-if="row.doc_version > 1" class="cell-secondary">第 {{ row.doc_version }} 版</span>
            </div>
          </td>
          <td class="cell-secondary">{{ formatBytes(row.size_bytes) }}</td>
          <td><EtlProgress :status="row.status" :error="row.error ?? null" /></td>
          <td>
            <div class="cell-actions">
              <!-- 後端在處理中的狀態會回 409（documents.py），這裡先把按鈕關掉——
                   兩邊的判定有時間差是正常的，真的撞上時錯誤訊息仍會顯示。 -->
              <InkButton variant="quiet" size="small" :disabled="!canReingest(row.status)" @click="reingest(row)">
                重新處理
              </InkButton>
              <InkConfirm
                text="刪除後，這份文件的內容與向量都會移除。確定嗎？"
                confirm-label="刪除"
                @confirm="remove(row)"
              >
                <InkButton variant="danger" size="small">刪除</InkButton>
              </InkConfirm>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<style scoped>
.view {
  display: flex;
  flex-direction: column;
  gap: 26px;
}

.head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
}

.head-titles {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.page-title {
  margin: 0;
}

/* 標題旁的落款小印 */
.title-seal {
  display: inline-block;
  width: 12px;
  height: 12px;
  background: var(--cinnabar);
  border-radius: 2px;
  transform: rotate(2deg);
}

.head-divider {
  width: 280px;
}

.page-subtitle {
  margin: 0;
}

.pending {
  font-size: 0.8125rem;
  color: var(--ink-4);
  padding-bottom: 6px;
}

.name-cell {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.col-size {
  width: 100px;
}

.col-status {
  width: 260px;
}

.col-actions {
  width: 190px;
}
</style>

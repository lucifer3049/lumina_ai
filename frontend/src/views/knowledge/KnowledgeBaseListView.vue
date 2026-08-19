<script setup lang="ts">
/**
 * 知識庫清單（1E-2；03 §2 views/knowledge）。
 *
 * view 只做三件事：載入、把事件交給 store、把失敗顯示出來。清單的維護、
 * 順序與一致性都在 store（03 §1：views 不直接 fetch）。
 */
import {
  NButton,
  NCard,
  NDataTable,
  NEmpty,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NPopconfirm,
  NSpace,
  useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { computed, h, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { useKnowledgeStore } from '@/stores/knowledge'
import type { KnowledgeBaseOut } from '@/types/models'
import { errorMessage } from '@/utils/errors'

const store = useKnowledgeStore()
const message = useMessage()

/** null = 新增；有值 = 編輯那一個。用同一個表單是因為欄位完全相同。 */
const editing = ref<KnowledgeBaseOut | null>(null)
const modalOpen = ref(false)
const form = ref({ name: '', description: '' })
const saving = ref(false)

const modalTitle = computed(() => (editing.value === null ? '新增知識庫' : '重新命名'))

onMounted(() => {
  void load()
})

async function load(): Promise<void> {
  try {
    await store.fetchKnowledgeBases()
  } catch (error) {
    message.error(errorMessage(error))
  }
}

function openCreate(): void {
  editing.value = null
  form.value = { name: '', description: '' }
  modalOpen.value = true
}

function openEdit(kb: KnowledgeBaseOut): void {
  editing.value = kb
  form.value = { name: kb.name, description: kb.description }
  modalOpen.value = true
}

/**
 * 回傳 false = 對話框不要關。naive-ui 的 positive-click 預設關閉，
 * 驗證失敗或後端拒絕時關掉的話，使用者剛打的字就沒了。
 */
async function save(): Promise<boolean> {
  const name = form.value.name.trim()
  if (name === '') {
    // 後端也會擋（422），但空名字不值得一次往返。
    message.warning('請輸入名稱')
    return false
  }
  saving.value = true
  try {
    if (editing.value === null) {
      await store.createKnowledgeBase({ name, description: form.value.description })
      message.success('已建立')
    } else {
      await store.updateKnowledgeBase(editing.value.id, {
        name,
        description: form.value.description,
      })
      message.success('已更新')
    }
    return true
  } catch (error) {
    message.error(errorMessage(error))
    return false
  } finally {
    saving.value = false
  }
}

async function remove(kb: KnowledgeBaseOut): Promise<void> {
  try {
    await store.deleteKnowledgeBase(kb.id)
    message.success('已刪除')
  } catch (error) {
    message.error(errorMessage(error))
  }
}

const columns = computed<DataTableColumns<KnowledgeBaseOut>>(() => [
  {
    title: '名稱',
    key: 'name',
    render: (row) =>
      h(
        RouterLink,
        { to: { name: 'knowledge-documents', params: { kbId: row.id } } },
        () => row.name,
      ),
  },
  { title: '說明', key: 'description' },
  { title: '文件數', key: 'document_count', width: 100 },
  {
    title: '',
    key: 'actions',
    width: 160,
    render: (row) =>
      h(NSpace, { size: 'small' }, () => [
        h(NButton, { size: 'small', quaternary: true, onClick: () => openEdit(row) }, () => '更名'),
        h(
          NPopconfirm,
          { onPositiveClick: () => void remove(row) },
          {
            // 刪除 KB 是 admin 權限且會連帶文件（09 §2.3），問一次再做。
            default: () => '刪除後，裡面的文件與已建立的向量都會一併移除。確定嗎？',
            trigger: () =>
              h(NButton, { size: 'small', quaternary: true, type: 'error' }, () => '刪除'),
          },
        ),
      ]),
  },
])
</script>

<template>
  <NCard title="知識庫">
    <template #header-extra>
      <NButton type="primary" size="small" @click="openCreate">新增知識庫</NButton>
    </template>

    <NDataTable
      :columns="columns"
      :data="store.knowledgeBases"
      :loading="store.loadingBases"
      :row-key="(row: KnowledgeBaseOut) => row.id"
      :bordered="false"
    >
      <template #empty>
        <NEmpty description="還沒有知識庫。建一個，然後把文件放進去。" />
      </template>
    </NDataTable>

    <NModal
      v-model:show="modalOpen"
      preset="dialog"
      :title="modalTitle"
      positive-text="儲存"
      negative-text="取消"
      :loading="saving"
      @positive-click="save"
    >
      <NForm class="form" @submit.prevent="save">
        <NFormItem label="名稱" required>
          <NInput v-model:value="form.name" maxlength="200" placeholder="例如：人事規章" />
        </NFormItem>
        <NFormItem label="說明">
          <NInput
            v-model:value="form.description"
            type="textarea"
            maxlength="2000"
            placeholder="這個知識庫收什麼內容"
          />
        </NFormItem>
      </NForm>
    </NModal>
  </NCard>
</template>

<style scoped>
.form {
  margin-top: 12px;
}
</style>

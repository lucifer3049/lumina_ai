<script setup lang="ts">
/**
 * 知識庫清單（1E-2；03 §2 views/knowledge）。
 *
 * view 只做三件事：載入、把事件交給 store、把失敗顯示出來。清單的維護、
 * 順序與一致性都在 store（03 §1：views 不直接 fetch）。
 */
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import BrushDivider from '@/components/ui/BrushDivider.vue'
import InkButton from '@/components/ui/InkButton.vue'
import InkConfirm from '@/components/ui/InkConfirm.vue'
import InkDialog from '@/components/ui/InkDialog.vue'
import InkEmpty from '@/components/ui/InkEmpty.vue'
import InkInput from '@/components/ui/InkInput.vue'
import InkSpinner from '@/components/ui/InkSpinner.vue'
import { useToast } from '@/composables/useToast'
import { useKnowledgeStore } from '@/stores/knowledge'
import type { KnowledgeBaseOut } from '@/types/models'
import { errorMessage } from '@/utils/errors'

const store = useKnowledgeStore()
const toast = useToast()

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
    toast.error(errorMessage(error))
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

/** 驗證失敗或後端拒絕時不關對話框，使用者剛打的字才不會沒了。 */
async function save(): Promise<void> {
  const name = form.value.name.trim()
  if (name === '') {
    // 後端也會擋（422），但空名字不值得一次往返。
    toast.warning('請輸入名稱')
    return
  }
  saving.value = true
  try {
    if (editing.value === null) {
      await store.createKnowledgeBase({ name, description: form.value.description })
      toast.success('已建立')
    } else {
      await store.updateKnowledgeBase(editing.value.id, {
        name,
        description: form.value.description,
      })
      toast.success('已更新')
    }
    modalOpen.value = false
  } catch (error) {
    toast.error(errorMessage(error))
  } finally {
    saving.value = false
  }
}

async function remove(kb: KnowledgeBaseOut): Promise<void> {
  try {
    await store.deleteKnowledgeBase(kb.id)
    toast.success('已刪除')
  } catch (error) {
    toast.error(errorMessage(error))
  }
}
</script>

<template>
  <section class="view ink-appear">
    <header class="head">
      <div class="head-titles">
        <h1 class="page-title">知識庫</h1>
        <BrushDivider class="head-divider" />
      </div>
      <InkButton variant="primary" size="small" @click="openCreate">新增知識庫</InkButton>
    </header>

    <InkSpinner v-if="store.loadingBases" />
    <InkEmpty
      v-else-if="store.knowledgeBases.length === 0"
      description="還沒有知識庫。建一個，然後把文件放進去。"
    />
    <table v-else class="ink-table">
      <thead>
        <tr>
          <th>名稱</th>
          <th>說明</th>
          <th class="col-count">文件數</th>
          <th class="col-actions"></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="kb in store.knowledgeBases" :key="kb.id">
          <td>
            <RouterLink :to="{ name: 'knowledge-documents', params: { kbId: kb.id } }">
              {{ kb.name }}
            </RouterLink>
          </td>
          <td class="cell-secondary">{{ kb.description }}</td>
          <td class="cell-secondary">{{ kb.document_count }}</td>
          <td>
            <div class="cell-actions">
              <InkButton variant="quiet" size="small" @click="openEdit(kb)">更名</InkButton>
              <!-- 刪除 KB 是 admin 權限且會連帶文件（09 §2.3），問一次再做。 -->
              <InkConfirm
                text="刪除後，裡面的文件與已建立的向量都會一併移除。確定嗎？"
                confirm-label="刪除"
                @confirm="remove(kb)"
              >
                <InkButton variant="danger" size="small">刪除</InkButton>
              </InkConfirm>
            </div>
          </td>
        </tr>
      </tbody>
    </table>

    <InkDialog v-model:open="modalOpen" :title="modalTitle">
      <form class="form" @submit.prevent="save">
        <div class="field">
          <label class="label" for="kb-name">名稱</label>
          <InkInput id="kb-name" v-model="form.name" :maxlength="200" placeholder="例如：人事規章" />
        </div>
        <div class="field">
          <label class="label" for="kb-description">說明</label>
          <InkInput
            id="kb-description"
            v-model="form.description"
            type="textarea"
            :maxlength="2000"
            :rows="3"
            placeholder="這個知識庫收什麼內容"
          />
        </div>
      </form>
      <template #actions>
        <InkButton variant="quiet" :disabled="saving" @click="modalOpen = false">取消</InkButton>
        <InkButton variant="primary" :loading="saving" @click="save">儲存</InkButton>
      </template>
    </InkDialog>
  </section>
</template>

<style scoped>
.view {
  display: flex;
  flex-direction: column;
  gap: 28px;
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
  gap: 10px;
}

.page-title {
  margin: 0;
}

.head-divider {
  width: 240px;
}

.col-count {
  width: 100px;
}

.col-actions {
  width: 170px;
}

.form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.label {
  font-size: 0.8125rem;
  font-weight: 500;
  letter-spacing: 0.1em;
  color: var(--ink-2);
}
</style>

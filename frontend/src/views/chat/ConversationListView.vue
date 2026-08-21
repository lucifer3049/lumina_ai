<script setup lang="ts">
/**
 * 對話清單（1E-3；09 §2.4）。
 *
 * 清單只列自己的（後端的擁有者制），所以這一頁不需要任何權限判斷——沒有的東西
 * 根本不會回來。
 */
import { onMounted } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import BrushDivider from '@/components/ui/BrushDivider.vue'
import InkButton from '@/components/ui/InkButton.vue'
import InkConfirm from '@/components/ui/InkConfirm.vue'
import InkEmpty from '@/components/ui/InkEmpty.vue'
import InkSpinner from '@/components/ui/InkSpinner.vue'
import { useToast } from '@/composables/useToast'
import { useChatStore } from '@/stores/chat'
import type { ConversationOut } from '@/types/models'
import { errorMessage } from '@/utils/errors'

const store = useChatStore()
const router = useRouter()
const toast = useToast()

onMounted(() => {
  void store.fetchConversations().catch((error: unknown) => {
    toast.error(errorMessage(error))
  })
})

async function startNew(): Promise<void> {
  try {
    // 標題留空：後端會用第一句話命名（09 §2.4）。這裡先給空字串而不是「新對話」，
    // 免得每一筆都叫同一個名字。
    const created = await store.createConversation({ kb_ids: [] })
    await router.push({ name: 'chat-conversation', params: { conversationId: created.id } })
  } catch (error) {
    toast.error(errorMessage(error))
  }
}

async function remove(conversation: ConversationOut): Promise<void> {
  try {
    await store.deleteConversation(conversation.id)
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
        <h1 class="page-title">對話</h1>
        <BrushDivider class="head-divider" />
        <p class="page-subtitle">以文會友，答疑解惑</p>
      </div>
      <InkButton variant="primary" size="small" @click="startNew">開始新對話</InkButton>
    </header>

    <InkSpinner v-if="store.loadingConversations" />
    <InkEmpty
      v-else-if="store.conversations.length === 0"
      description="還沒有對話。開一個，問問看知識庫裡有什麼。"
    />
    <table v-else class="ink-table">
      <thead>
        <tr>
          <th>標題</th>
          <th class="col-count">訊息數</th>
          <th class="col-actions"></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in store.conversations" :key="row.id">
          <td>
            <RouterLink :to="{ name: 'chat-conversation', params: { conversationId: row.id } }">
              {{ row.title === '' ? '（未命名）' : row.title }}
            </RouterLink>
          </td>
          <td class="cell-secondary">{{ row.message_count }}</td>
          <td>
            <div class="cell-actions">
              <InkConfirm
                text="刪除後這場對話的訊息都會消失。確定嗎？"
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
  gap: 8px;
}

.page-title {
  margin: 0;
}

.head-divider {
  width: 210px;
}

.page-subtitle {
  margin: 0;
}

.col-count {
  width: 100px;
}

.col-actions {
  width: 100px;
}
</style>

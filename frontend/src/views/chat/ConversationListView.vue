<script setup lang="ts">
/**
 * 對話清單（1E-3；09 §2.4）。
 *
 * 清單只列自己的（後端的擁有者制），所以這一頁不需要任何權限判斷——沒有的東西
 * 根本不會回來。
 */
import { NButton, NCard, NDataTable, NEmpty, NPopconfirm, NSpace, useMessage } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { computed, h, onMounted } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { useChatStore } from '@/stores/chat'
import type { ConversationOut } from '@/types/models'
import { errorMessage } from '@/utils/errors'

const store = useChatStore()
const router = useRouter()
const message = useMessage()

onMounted(() => {
  void store.fetchConversations().catch((error: unknown) => {
    message.error(errorMessage(error))
  })
})

async function startNew(): Promise<void> {
  try {
    // 標題留空：後端會用第一句話命名（09 §2.4）。這裡先給空字串而不是「新對話」，
    // 免得每一筆都叫同一個名字。
    const created = await store.createConversation({ kb_ids: [] })
    await router.push({ name: 'chat-conversation', params: { conversationId: created.id } })
  } catch (error) {
    message.error(errorMessage(error))
  }
}

async function remove(conversation: ConversationOut): Promise<void> {
  try {
    await store.deleteConversation(conversation.id)
    message.success('已刪除')
  } catch (error) {
    message.error(errorMessage(error))
  }
}

const columns = computed<DataTableColumns<ConversationOut>>(() => [
  {
    title: '標題',
    key: 'title',
    render: (row) =>
      h(
        RouterLink,
        { to: { name: 'chat-conversation', params: { conversationId: row.id } } },
        () => (row.title === '' ? '（未命名）' : row.title),
      ),
  },
  { title: '訊息數', key: 'message_count', width: 100 },
  {
    title: '',
    key: 'actions',
    width: 90,
    render: (row) =>
      h(
        NPopconfirm,
        { onPositiveClick: () => void remove(row) },
        {
          default: () => '刪除後這場對話的訊息都會消失。確定嗎？',
          trigger: () =>
            h(NButton, { size: 'small', quaternary: true, type: 'error' }, () => '刪除'),
        },
      ),
  },
])
</script>

<template>
  <NCard title="對話">
    <template #header-extra>
      <NSpace>
        <NButton type="primary" size="small" @click="startNew">開始新對話</NButton>
      </NSpace>
    </template>

    <NDataTable
      :columns="columns"
      :data="store.conversations"
      :loading="store.loadingConversations"
      :row-key="(row: ConversationOut) => row.id"
      :bordered="false"
    >
      <template #empty>
        <NEmpty description="還沒有對話。開一個，問問看知識庫裡有什麼。" />
      </template>
    </NDataTable>
  </NCard>
</template>

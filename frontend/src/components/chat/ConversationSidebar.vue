<script setup lang="ts">
/**
 * 對話側欄（ChatGPT 版式、青綠皮膚）：新對話、搜尋、按日期分組的對話紀錄。
 *
 * 搜尋是前端過濾**標題**——訊息內文的全文搜尋需要後端端點（pgroonga 已在牌桌上），
 * 屬範圍外，記回報清單。清單資料在這裡抓：側欄跨 `/chat` 與 `/chat/:id` 常駐，
 * 由它負責載入，兩條路由就不用各自抓一次。
 */
import { computed, onMounted, ref } from 'vue'

import InkButton from '@/components/ui/InkButton.vue'
import InkConfirm from '@/components/ui/InkConfirm.vue'
import InkInput from '@/components/ui/InkInput.vue'
import InkSpinner from '@/components/ui/InkSpinner.vue'
import { useToast } from '@/composables/useToast'
import { useChatStore } from '@/stores/chat'
import type { ConversationOut } from '@/types/models'
import { filterConversations, groupConversations } from '@/utils/conversationGroups'
import { errorMessage } from '@/utils/errors'

const props = defineProps<{ activeId: string | null }>()
const emit = defineEmits<{
  (event: 'select', conversationId: string): void
  (event: 'create'): void
  (event: 'deleted', conversationId: string): void
}>()

const store = useChatStore()
const toast = useToast()
const query = ref('')

const groups = computed(() =>
  groupConversations(filterConversations(store.conversations, query.value)),
)

onMounted(() => {
  void store.fetchConversations().catch((error: unknown) => {
    toast.error(errorMessage(error))
  })
})

async function remove(conversation: ConversationOut): Promise<void> {
  try {
    await store.deleteConversation(conversation.id)
    toast.success('已刪除')
    emit('deleted', conversation.id)
  } catch (error) {
    toast.error(errorMessage(error))
  }
}
</script>

<template>
  <aside class="sidebar">
    <div class="tools">
      <InkButton variant="primary" size="small" block @click="emit('create')">新對話</InkButton>
      <InkInput v-model="query" placeholder="搜尋對話" />
    </div>

    <nav class="list" aria-label="對話紀錄">
      <InkSpinner v-if="store.loadingConversations && store.conversations.length === 0" />
      <p v-else-if="groups.length === 0" class="none">
        {{ query.trim() === '' ? '還沒有對話' : '沒有符合的對話' }}
      </p>
      <template v-else>
        <section v-for="group in groups" :key="group.label" class="group">
          <h2 class="group-label">{{ group.label }}</h2>
          <div
            v-for="item in group.items"
            :key="item.id"
            class="row"
            :class="{ active: item.id === props.activeId }"
          >
            <button type="button" class="row-title" @click="emit('select', item.id)">
              {{ item.title === '' ? '（未命名）' : item.title }}
            </button>
            <InkConfirm
              text="刪除後這場對話的訊息都會消失。確定嗎？"
              confirm-label="刪除"
              @confirm="remove(item)"
            >
              <button type="button" class="row-delete" aria-label="刪除對話">✕</button>
            </InkConfirm>
          </div>
        </section>
      </template>
    </nav>
  </aside>
</template>

<style scoped>
.sidebar {
  width: 264px;
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 22px 14px 14px;
  border-right: 1px solid var(--paper-4);
  box-sizing: border-box;
}

.tools {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 0 4px;
}

.list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 0 4px 8px;
}

.none {
  margin: 8px 0 0;
  font-size: 0.8125rem;
  color: var(--ink-4);
  text-align: center;
}

.group {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.group-label {
  margin: 0 0 4px;
  padding: 0 8px;
  font-family: var(--font-body);
  font-size: 0.6875rem;
  font-weight: 400;
  letter-spacing: 0.22em;
  color: var(--ink-4);
}

.row {
  position: relative;
  display: flex;
  align-items: center;
  border-radius: var(--radius-a);
  transition: background-color var(--dur-fast) var(--ease-ink);
}

.row:hover {
  background: color-mix(in srgb, var(--paper-3) 70%, transparent);
}

.row.active {
  background: color-mix(in srgb, var(--paper-3) 90%, transparent);
}

.row-title {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  padding: 9px 26px 9px 8px;
  font-family: var(--font-body);
  font-size: 0.8125rem;
  color: var(--ink-2);
  text-align: left;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.row.active .row-title {
  color: var(--ink-1);
}

/* 刪除鈕平時隱形：清單是閱讀面，動作只在指到那一列時現身 */
.row-delete {
  position: absolute;
  right: 4px;
  width: 20px;
  height: 20px;
  border: none;
  border-radius: var(--radius-a);
  background: transparent;
  color: var(--ink-4);
  font-size: 0.6875rem;
  line-height: 1;
  cursor: pointer;
  opacity: 0;
  transition:
    opacity var(--dur-fast) var(--ease-ink),
    color var(--dur-fast) var(--ease-ink);
}

.row:hover .row-delete,
.row-delete:focus-visible {
  opacity: 1;
}

.row-delete:hover {
  color: var(--cinnabar);
}
</style>

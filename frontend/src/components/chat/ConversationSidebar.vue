<script setup lang="ts">
/**
 * 對話側欄（ChatGPT 版式、青綠皮膚）：新對話、搜尋、按日期分組的對話紀錄，
 * 可收合成一條 icon 窄軌（同 ChatGPT 的側欄開關）。
 *
 * 收合狀態記在 localStorage：這是「這台裝置怎麼用」的偏好，不是資料，
 * 不進後端。讀寫都包 try——私隱模式下 localStorage 會丟例外，偏好丟了就丟了。
 *
 * 搜尋是前端過濾**標題**——訊息內文的全文搜尋需要後端端點（pgroonga 已在牌桌上），
 * 屬範圍外，記回報清單。清單資料在這裡抓：側欄跨 `/chat` 與 `/chat/:id` 常駐，
 * 由它負責載入，兩條路由就不用各自抓一次。
 */
import { computed, nextTick, onMounted, ref } from 'vue'

import InkConfirm from '@/components/ui/InkConfirm.vue'
import InkSpinner from '@/components/ui/InkSpinner.vue'
import { useToast } from '@/composables/useToast'
import { useChatStore } from '@/stores/chat'
import type { ConversationOut } from '@/types/models'
import { filterConversations, groupConversations } from '@/utils/conversationGroups'
import { errorMessage } from '@/utils/errors'

const STORAGE_KEY = 'chat-sidebar-collapsed'

const props = defineProps<{ activeId: string | null }>()
const emit = defineEmits<{
  (event: 'select', conversationId: string): void
  (event: 'create'): void
  (event: 'deleted', conversationId: string): void
}>()

const store = useChatStore()
const toast = useToast()
const query = ref('')
const collapsed = ref(false)
const searchWrap = ref<HTMLElement | null>(null)

try {
  collapsed.value = localStorage.getItem(STORAGE_KEY) === '1'
} catch {
  // 私隱模式等情況：不持久化，開著就好
}

const groups = computed(() =>
  groupConversations(filterConversations(store.conversations, query.value)),
)

onMounted(() => {
  void store.fetchConversations().catch((error: unknown) => {
    toast.error(errorMessage(error))
  })
})

function setCollapsed(value: boolean): void {
  collapsed.value = value
  try {
    localStorage.setItem(STORAGE_KEY, value ? '1' : '0')
  } catch {
    // 同上：偏好存不了就算了
  }
}

/** 窄軌上點搜尋＝「我要找東西」：展開後直接把游標放進搜尋框。 */
async function expandToSearch(): Promise<void> {
  setCollapsed(false)
  await nextTick()
  searchWrap.value?.querySelector('input')?.focus()
}

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
  <aside class="sidebar" :class="{ 'sidebar--rail': collapsed }">
    <!-- 收合：一條 icon 窄軌 -->
    <template v-if="collapsed">
      <button
        type="button"
        class="icon-button"
        aria-label="展開對話清單"
        :aria-expanded="false"
        @click="setCollapsed(false)"
      >
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.7"
          stroke-linecap="round"
          aria-hidden="true"
        >
          <rect x="3.5" y="4.5" width="17" height="15" rx="3.5"></rect>
          <path d="M9.5 4.5 V 19.5"></path>
        </svg>
      </button>
      <button type="button" class="icon-button" aria-label="新對話" @click="emit('create')">
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.7"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <path
            d="M12 5 H 6.5 A 2.5 2.5 0 0 0 4 7.5 V 17.5 A 2.5 2.5 0 0 0 6.5 20 H 16.5 A 2.5 2.5 0 0 0 19 17.5 V 12"
          ></path>
          <path d="M17.8 3.6 a 1.9 1.9 0 0 1 2.7 2.7 L 12.7 14 L 9 15 L 10 11.3 Z"></path>
        </svg>
      </button>
      <button type="button" class="icon-button" aria-label="搜尋對話" @click="expandToSearch">
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.7"
          stroke-linecap="round"
          aria-hidden="true"
        >
          <circle cx="11" cy="11" r="6.5"></circle>
          <path d="M15.8 15.8 L 20 20"></path>
        </svg>
      </button>
    </template>

    <!-- 展開：完整清單。工具不畫框（同 GPT）：icon＋文字的純列，hover 才有底色。 -->
    <template v-else>
      <div class="tools">
        <div class="tools-row">
          <button type="button" class="tool-button" @click="emit('create')">
            <svg
              width="17"
              height="17"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.7"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <path
                d="M12 5 H 6.5 A 2.5 2.5 0 0 0 4 7.5 V 17.5 A 2.5 2.5 0 0 0 6.5 20 H 16.5 A 2.5 2.5 0 0 0 19 17.5 V 12"
              ></path>
              <path d="M17.8 3.6 a 1.9 1.9 0 0 1 2.7 2.7 L 12.7 14 L 9 15 L 10 11.3 Z"></path>
            </svg>
            新對話
          </button>
          <button
            type="button"
            class="icon-button"
            aria-label="收合對話清單"
            :aria-expanded="true"
            @click="setCollapsed(true)"
          >
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.7"
              stroke-linecap="round"
              aria-hidden="true"
            >
              <rect x="3.5" y="4.5" width="17" height="15" rx="3.5"></rect>
              <path d="M9.5 4.5 V 19.5"></path>
            </svg>
          </button>
        </div>
        <label ref="searchWrap" class="tool-search">
          <svg
            width="17"
            height="17"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linecap="round"
            aria-hidden="true"
          >
            <circle cx="11" cy="11" r="6.5"></circle>
            <path d="M15.8 15.8 L 20 20"></path>
          </svg>
          <input v-model="query" class="tool-search-input" type="text" placeholder="搜尋對話" />
        </label>
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
    </template>
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
  transition: width var(--dur-med) var(--ease-ink);
}

.sidebar--rail {
  width: 60px;
  align-items: center;
  gap: 8px;
  padding: 22px 10px 14px;
}

.icon-button {
  flex: none;
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: var(--radius-a);
  background: transparent;
  color: var(--ink-3);
  cursor: pointer;
  transition:
    background-color var(--dur-fast) var(--ease-ink),
    color var(--dur-fast) var(--ease-ink);
}

.icon-button:hover {
  background: color-mix(in srgb, var(--paper-3) 70%, transparent);
  color: var(--ink-1);
}

.tools {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 0 4px;
}

.tools-row {
  display: flex;
  align-items: center;
  gap: 4px;
}

/* 無框工具列（同 GPT）：與清單列同一套 hover 語言 */
.tool-button {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  height: 36px;
  padding: 0 8px;
  border: none;
  border-radius: var(--radius-a);
  background: transparent;
  font-family: var(--font-body);
  font-size: 0.8125rem;
  letter-spacing: 0.08em;
  color: var(--ink-2);
  text-align: left;
  cursor: pointer;
  transition:
    background-color var(--dur-fast) var(--ease-ink),
    color var(--dur-fast) var(--ease-ink);
}

.tool-button:hover {
  background: color-mix(in srgb, var(--paper-3) 70%, transparent);
  color: var(--ink-1);
}

.tool-search {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 36px;
  padding: 0 8px;
  border-radius: var(--radius-a);
  color: var(--ink-3);
  cursor: text;
  transition: background-color var(--dur-fast) var(--ease-ink);
}

.tool-search:hover,
.tool-search:focus-within {
  background: color-mix(in srgb, var(--paper-3) 70%, transparent);
}

.tool-search-input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  padding: 0;
  font-family: var(--font-body);
  font-size: 0.8125rem;
  color: var(--ink-1);
}

.tool-search-input::placeholder {
  color: var(--ink-4);
}

.tool-search-input:focus {
  outline: none;
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

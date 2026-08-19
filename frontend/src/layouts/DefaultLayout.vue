<script setup lang="ts">
/**
 * 主應用外框（03 §2）：sidebar + header。
 *
 * 選單目前只有首頁；知識庫（1E-2）與對話（1E-3）的項目隨各自的工作包進來
 * ——先放假選項會讓「點了沒反應」看起來像壞掉。
 */
import { NButton, NLayout, NLayoutContent, NLayoutHeader, NLayoutSider, NMenu } from 'naive-ui'
import type { MenuOption } from 'naive-ui'
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

const menuOptions: MenuOption[] = [{ label: '首頁', key: 'home' }]
const activeKey = computed(() => String(router.currentRoute.value.name ?? ''))

function onMenuSelect(key: string): void {
  void router.push({ name: key })
}

async function onLogout(): Promise<void> {
  await auth.logout()
  // logout 本地必清（斷網也一樣），所以這裡永遠走得到登入頁。
  await router.push({ name: 'login' })
}
</script>

<template>
  <NLayout has-sider class="default-layout">
    <NLayoutSider bordered :width="220" :native-scrollbar="false">
      <div class="brand">Lumina AI</div>
      <NMenu :options="menuOptions" :value="activeKey" @update:value="onMenuSelect" />
    </NLayoutSider>
    <NLayout>
      <NLayoutHeader bordered class="header">
        <span class="user-name">{{ auth.user?.display_name ?? '' }}</span>
        <NButton quaternary size="small" @click="onLogout">登出</NButton>
      </NLayoutHeader>
      <NLayoutContent content-style="padding: 24px;">
        <slot />
      </NLayoutContent>
    </NLayout>
  </NLayout>
</template>

<style scoped>
.default-layout {
  min-height: 100vh;
}

.brand {
  padding: 16px 24px;
  font-weight: 600;
}

.header {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  height: 48px;
  padding: 0 24px;
}

.user-name {
  font-size: 0.875rem;
}
</style>

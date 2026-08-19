<script setup lang="ts">
/**
 * 主應用外框（03 §2）：sidebar + header。
 *
 * 選單隨工作包長出來：首頁（1E-1）、知識庫（1E-2），對話留給 1E-3
 * ——先放假選項會讓「點了沒反應」看起來像壞掉。
 */
import { NButton, NLayout, NLayoutContent, NLayoutHeader, NLayoutSider, NMenu } from 'naive-ui'
import type { MenuOption } from 'naive-ui'
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

const menuOptions: MenuOption[] = [
  { label: '首頁', key: 'home' },
  { label: '知識庫', key: 'knowledge' },
]
// 選單 key 就是 route name（onMenuSelect 直接 push 它）。文件頁是知識庫的子頁，
// 停在那裡時選單仍要亮著「知識庫」，否則使用者會覺得自己離開了那一區。
const activeKey = computed(() => {
  const name = String(router.currentRoute.value.name ?? '')
  return name === 'knowledge-documents' ? 'knowledge' : name
})

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

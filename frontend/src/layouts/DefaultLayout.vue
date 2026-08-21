<script setup lang="ts">
/**
 * 主應用外框（03 §2）：sidebar + header，樣式為「卷軸邊欄」——側欄無底色、
 * 細墨線分隔；active 記號是朱砂小點（落款用法）。
 *
 * 選單隨工作包長出來：首頁（1E-1）、知識庫（1E-2）、對話（1E-3）。
 * 工作頁的背景場景只留右下遠山一角（設計語言 §視覺焦點：登入濃、工作淡）。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import SealMark from '@/components/ui/SealMark.vue'
import ThemeToggle from '@/components/ui/ThemeToggle.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

const menuOptions = [
  { label: '首頁', key: 'home' },
  { label: '對話', key: 'chat' },
  { label: '知識庫', key: 'knowledge' },
]
// 選單 key 就是 route name（onMenuSelect 直接 push 它）。文件頁是知識庫的子頁，
// 停在那裡時選單仍要亮著「知識庫」，否則使用者會覺得自己離開了那一區。
const PARENT_OF: Record<string, string> = {
  'knowledge-documents': 'knowledge',
  'chat-conversation': 'chat',
}
const activeKey = computed(() => {
  const name = String(router.currentRoute.value.name ?? '')
  return PARENT_OF[name] ?? name
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
  <div class="default-layout">
    <!-- 右下青綠遠山一角（無描邊：山色向霧溶解） -->
    <svg viewBox="0 0 760 240" class="corner-scene" preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <filter id="dl-soft"><feGaussianBlur stdDeviation="4"></feGaussianBlur></filter>
        <linearGradient id="dl-peak-a" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--peak-mid)"></stop>
          <stop offset="1" style="stop-color: var(--peak-mid); stop-opacity: 0"></stop>
        </linearGradient>
        <linearGradient id="dl-peak-b" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--peak-near)"></stop>
          <stop offset="1" style="stop-color: var(--peak-near); stop-opacity: 0"></stop>
        </linearGradient>
        <linearGradient id="dl-mist" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--paper-1); stop-opacity: 0"></stop>
          <stop offset="0.55" style="stop-color: var(--paper-1); stop-opacity: 0.9"></stop>
          <stop offset="1" style="stop-color: var(--paper-1); stop-opacity: 0.3"></stop>
        </linearGradient>
      </defs>
      <path
        d="M40 190 C 170 116, 310 100, 450 136 C 560 162, 670 160, 760 148 L 760 240 L 40 240 Z"
        fill="url(#dl-peak-a)"
        opacity="0.5"
        filter="url(#dl-soft)"
      ></path>
      <path
        d="M330 208 C 460 168, 600 162, 760 186 L 760 240 L 330 240 Z"
        fill="url(#dl-peak-b)"
        opacity="0.4"
      ></path>
      <rect x="0" y="160" width="760" height="80" fill="url(#dl-mist)"></rect>
    </svg>

    <aside class="sidebar">
      <div class="brand">
        <SealMark char="智" :size="27" />
        <span class="brand-name">Lumina AI</span>
      </div>
      <nav class="menu">
        <button
          v-for="option in menuOptions"
          :key="option.key"
          type="button"
          class="menu-item"
          :class="{ active: activeKey === option.key }"
          @click="onMenuSelect(option.key)"
        >
          <span class="mark" aria-hidden="true"></span>
          {{ option.label }}
        </button>
      </nav>
    </aside>

    <div class="main">
      <header class="header">
        <ThemeToggle />
        <span class="user-name">{{ auth.user?.display_name ?? '' }}</span>
        <button type="button" class="logout" @click="onLogout">登出</button>
      </header>
      <main class="content">
        <slot />
      </main>
    </div>
  </div>
</template>

<style scoped>
.default-layout {
  position: relative;
  display: flex;
  min-height: 100vh;
  overflow: hidden;
}

.corner-scene {
  position: fixed;
  right: 0;
  bottom: 0;
  width: min(760px, 60vw);
  height: 240px;
  pointer-events: none;
}

.sidebar {
  position: relative;
  width: 210px;
  flex-shrink: 0;
  border-right: 1px solid var(--paper-4);
  display: flex;
  flex-direction: column;
  gap: 34px;
  padding: 26px 16px;
  box-sizing: border-box;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 10px;
}

.brand-name {
  font-family: var(--font-serif);
  font-size: 1.0625rem;
  font-weight: 700;
  letter-spacing: 0.05em;
  color: var(--ink-1);
}

.menu {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.menu-item {
  display: flex;
  align-items: center;
  gap: 9px;
  height: 44px;
  padding: 0 10px;
  border: none;
  background: transparent;
  font-family: var(--font-body);
  font-size: 0.875rem;
  letter-spacing: 0.14em;
  color: var(--ink-3);
  text-align: left;
  cursor: pointer;
  border-radius: var(--radius-a);
  transition:
    background-color var(--dur-med) var(--ease-ink),
    color var(--dur-med) var(--ease-ink);
}

.menu-item:hover {
  background: color-mix(in srgb, var(--paper-3) 70%, transparent);
  color: var(--ink-1);
}

.menu-item .mark {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: transparent;
  transition: background-color var(--dur-med) var(--ease-ink);
}

.menu-item.active {
  color: var(--ink-1);
  font-weight: 500;
}

.menu-item.active .mark {
  border-radius: 60% 40% 55% 45%;
  background: var(--cinnabar);
}

.main {
  position: relative;
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.header {
  height: 54px;
  flex-shrink: 0;
  border-bottom: 1px solid var(--paper-4);
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 16px;
  padding: 0 32px;
}

.user-name {
  font-size: 0.8125rem;
  color: var(--ink-4);
}

.logout {
  border: none;
  background: transparent;
  padding: 12px 8px;
  font-family: var(--font-body);
  font-size: 0.8125rem;
  letter-spacing: 0.1em;
  color: var(--ink-3);
  cursor: pointer;
  transition: color var(--dur-med) var(--ease-ink);
}

.logout:hover {
  color: var(--ink-1);
}

.content {
  flex: 1;
  min-height: 0;
  padding: 40px 56px;
}

@media (max-width: 900px) {
  .content {
    padding: 24px 20px;
  }
}
</style>

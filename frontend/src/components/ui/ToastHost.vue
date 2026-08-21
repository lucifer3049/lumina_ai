<script setup lang="ts">
/**
 * Toast 出口，掛在 App.vue 最外層一次。樣式對映設計語言：
 * 成功＝青瓷、失敗＝朱砂（朱只在這種「需要被看見」的時刻出現）、其餘墨色。
 */
import { dismiss, toastItems } from '@/composables/useToast'
</script>

<template>
  <div class="toast-host" aria-live="polite">
    <div
      v-for="item in toastItems"
      :key="item.id"
      class="toast ink-appear"
      :class="`toast--${item.type}`"
      role="status"
      @click="dismiss(item.id)"
    >
      {{ item.text }}
    </div>
  </div>
</template>

<style scoped>
.toast-host {
  position: fixed;
  top: 18px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 1000;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  pointer-events: none;
}

.toast {
  pointer-events: auto;
  max-width: 32rem;
  padding: 9px 18px;
  font-size: 0.875rem;
  line-height: 1.6;
  color: var(--ink-1);
  background: var(--paper-2);
  border: 1px solid var(--ink-5);
  border-radius: var(--radius-b);
  box-shadow: var(--shadow-mist);
  cursor: pointer;
}

.toast--success {
  border-color: color-mix(in srgb, var(--celadon) 90%, transparent);
  color: var(--celadon-ink);
}

.toast--error {
  border-color: color-mix(in srgb, var(--cinnabar) 65%, transparent);
  color: var(--cinnabar);
}

.toast--warning {
  border-color: var(--ink-4);
  color: var(--ink-2);
}
</style>

<script setup lang="ts">
/**
 * 晝／夜切換鈕：楷體單字——白天顯示「夜」（按了入夜），夜裡顯示「晝」。
 * 文人的開關不畫太陽月亮圖示，一個字就夠。
 */
import { computed } from 'vue'

import { useTheme } from '@/composables/useTheme'

const { theme, toggle } = useTheme()
const glyph = computed(() => (theme.value === 'dark' ? '晝' : '夜'))
const label = computed(() => (theme.value === 'dark' ? '切換為白晝' : '切換為夜色'))
</script>

<template>
  <button type="button" class="theme-toggle" :aria-label="label" :title="label" @click="toggle">
    {{ glyph }}
  </button>
</template>

<style scoped>
.theme-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: 1px solid var(--ink-5);
  border-radius: 50% 46% 52% 48%;
  background: color-mix(in srgb, var(--paper-2) 70%, transparent);
  font-family: var(--font-kai);
  font-size: 15px;
  color: var(--ink-2);
  cursor: pointer;
  transition:
    background-color var(--dur-med) var(--ease-ink),
    color var(--dur-med) var(--ease-ink),
    border-color var(--dur-med) var(--ease-ink);
}

.theme-toggle:hover {
  background: var(--paper-3);
  color: var(--ink-1);
  border-color: var(--ink-4);
}
</style>

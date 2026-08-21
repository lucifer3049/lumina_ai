<script setup lang="ts">
/**
 * Phase 0 的佔位頁——唯一目的是證明骨架（Vite + router + pinia）真的跑得起來。
 * 首頁的實際內容（儀表板或導引）排在後續工作包；此處僅套上設計語言的標題階層。
 */
import BrushDivider from '@/components/ui/BrushDivider.vue'
</script>

<template>
  <main class="placeholder ink-appear">
    <h1 class="page-title">Lumina AI</h1>
    <BrushDivider class="divider" />
    <p class="page-subtitle">智啟千年 · 知識如水</p>
    <p class="note">從左側選單進入「對話」或「知識庫」開始使用。</p>
  </main>
</template>

<style scoped>
.placeholder {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-width: 40rem;
  padding: 2rem 0;
}

.page-title {
  margin: 0;
}

.divider {
  width: 240px;
}

.page-subtitle {
  margin: 0;
}

.note {
  margin: 18px 0 0;
  font-size: 0.875rem;
  color: var(--ink-3);
}
</style>

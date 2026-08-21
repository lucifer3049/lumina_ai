<script setup lang="ts">
/**
 * 對話框。行為（焦點鎖定、Esc、點外關閉、aria）交給 Reka UI 的 Dialog
 * primitives，樣式全部自己寫——這正是選 headless 的理由（03 §8 v1.3）。
 * 面板用書畫裝裱式雙線框，對齊視覺稿的登入面。
 */
import { DialogContent, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from 'reka-ui'

const props = defineProps<{ open: boolean; title: string }>()
const emit = defineEmits<{ (event: 'update:open', value: boolean): void }>()
</script>

<template>
  <DialogRoot :open="props.open" @update:open="emit('update:open', $event)">
    <DialogPortal>
      <DialogOverlay class="overlay" />
      <DialogContent class="content" :aria-describedby="undefined">
        <div class="inner">
          <DialogTitle class="title">{{ props.title }}</DialogTitle>
          <div class="body"><slot /></div>
          <div class="actions"><slot name="actions" /></div>
        </div>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  z-index: 100;
  background: rgba(16, 20, 18, 0.42);
  animation: ink-appear var(--dur-med) var(--ease-ink) both;
}

.content {
  position: fixed;
  top: 50%;
  left: 50%;
  z-index: 101;
  transform: translate(-50%, -50%);
  width: min(26rem, calc(100vw - 2rem));
  background: var(--paper-2);
  border: 1px solid var(--ink-5);
  padding: 5px;
  box-shadow: var(--shadow-mist);
  animation: ink-appear var(--dur-med) var(--ease-ink) both;
}

.inner {
  border: 1px solid var(--ink-5);
  padding: 24px 28px 28px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.title {
  margin: 0;
  font-family: var(--font-serif);
  font-size: 1.125rem;
  font-weight: 700;
  letter-spacing: 0.14em;
  color: var(--ink-1);
}

.body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>

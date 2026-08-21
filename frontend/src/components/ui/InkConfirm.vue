<script setup lang="ts">
/**
 * 確認框（取代 NPopconfirm）：破壞性操作先問一次。用 AlertDialog 而不是
 * Popover——它是 modal、預設焦點落在按鈕上、Esc 取消，誤觸成本最低。
 */
import {
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogOverlay,
  AlertDialogPortal,
  AlertDialogRoot,
  AlertDialogTitle,
  AlertDialogTrigger,
} from 'reka-ui'

const props = withDefaults(defineProps<{ text: string; confirmLabel?: string }>(), {
  confirmLabel: '確定',
})
const emit = defineEmits<{ (event: 'confirm'): void }>()
</script>

<template>
  <AlertDialogRoot>
    <AlertDialogTrigger as-child>
      <slot />
    </AlertDialogTrigger>
    <AlertDialogPortal>
      <AlertDialogOverlay class="overlay" />
      <AlertDialogContent class="content">
        <div class="inner">
          <AlertDialogTitle class="title">請確認</AlertDialogTitle>
          <AlertDialogDescription class="text">{{ props.text }}</AlertDialogDescription>
          <div class="actions">
            <AlertDialogCancel as-child>
              <button type="button" class="cancel">取消</button>
            </AlertDialogCancel>
            <AlertDialogAction as-child>
              <button type="button" class="confirm" @click="emit('confirm')">
                {{ props.confirmLabel }}
              </button>
            </AlertDialogAction>
          </div>
        </div>
      </AlertDialogContent>
    </AlertDialogPortal>
  </AlertDialogRoot>
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
  width: min(24rem, calc(100vw - 2rem));
  background: var(--paper-2);
  border: 1px solid var(--ink-5);
  padding: 5px;
  box-shadow: var(--shadow-mist);
  animation: ink-appear var(--dur-med) var(--ease-ink) both;
}

.inner {
  border: 1px solid var(--ink-5);
  padding: 22px 26px 24px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.title {
  margin: 0;
  font-family: var(--font-serif);
  font-size: 1rem;
  font-weight: 700;
  letter-spacing: 0.14em;
  color: var(--ink-1);
}

.text {
  margin: 0;
  font-size: 0.875rem;
  line-height: 1.8;
  color: var(--ink-2);
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding-top: 4px;
}

.cancel,
.confirm {
  min-height: 40px;
  padding: 0 16px;
  font-family: var(--font-body);
  font-size: 0.8125rem;
  letter-spacing: 0.14em;
  cursor: pointer;
  border-radius: var(--radius-b);
  transition:
    background-color var(--dur-med) var(--ease-ink),
    color var(--dur-med) var(--ease-ink);
}

.cancel {
  border: 1px solid var(--ink-5);
  background: transparent;
  color: var(--ink-3);
}

.cancel:hover {
  background: var(--paper-3);
}

/* 破壞性確認＝需要被看見的時刻，朱砂在此出現。 */
.confirm {
  border: 1px solid color-mix(in srgb, var(--cinnabar) 65%, transparent);
  background: color-mix(in srgb, var(--cinnabar) 7%, transparent);
  color: var(--cinnabar);
}

.confirm:hover {
  background: color-mix(in srgb, var(--cinnabar) 15%, transparent);
}
</style>

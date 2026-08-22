<script setup lang="ts">
/**
 * 按鈕。primary 是筆觸墨塊（SVG 不規則邊當背景），danger 平時是淡墨文字、
 * hover 才轉朱砂——朱是落款，不是常駐的 UI 顏色（設計語言）。
 */
const props = withDefaults(
  defineProps<{
    variant?: 'primary' | 'secondary' | 'quiet' | 'danger'
    size?: 'small' | 'medium'
    disabled?: boolean
    loading?: boolean
    block?: boolean
    attrType?: 'button' | 'submit'
  }>(),
  { variant: 'secondary', size: 'medium', attrType: 'button' },
)

const emit = defineEmits<{ (event: 'click', payload: MouseEvent): void }>()

function onClick(event: MouseEvent): void {
  if (props.disabled === true || props.loading === true) {
    return
  }
  emit('click', event)
}
</script>

<template>
  <button
    :type="props.attrType"
    class="ink-button"
    :class="[`ink-button--${props.variant}`, `ink-button--${props.size}`, { block: props.block }]"
    :disabled="props.disabled || props.loading"
    @click="onClick"
  >
    <span v-if="props.loading" class="dot" aria-hidden="true"></span>
    <span class="label"><slot /></span>
  </button>
</template>

<style scoped>
.ink-button {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border: none;
  font-family: var(--font-body);
  font-size: 0.875rem;
  letter-spacing: 0.14em;
  cursor: pointer;
  background: transparent;
  color: var(--ink-2);
  border-radius: var(--radius-b);
  transition:
    background-color var(--dur-med) var(--ease-ink),
    color var(--dur-med) var(--ease-ink),
    box-shadow var(--dur-med) var(--ease-ink),
    opacity var(--dur-med) var(--ease-ink);
}

.ink-button--medium {
  min-height: 44px;
  padding: 0 20px;
}

.ink-button--small {
  min-height: 36px;
  padding: 0 12px;
  font-size: 0.8125rem;
}

.block {
  width: 100%;
}

.ink-button:disabled {
  cursor: default;
  color: var(--ink-5);
}

/* 主按鈕：平面黛青（v4 校正——不再用筆觸墨塊，形體乾淨，質感靠色與光） */
.ink-button--primary {
  color: var(--accent-deep-ink);
  background: var(--accent-deep);
  border-radius: var(--radius-b);
  letter-spacing: 0.24em;
  text-indent: 0.24em;
}

/* hover＝一抹更深的青從左側緩緩滲開 */
.ink-button--primary::after {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: inherit;
  background: radial-gradient(ellipse at left center, rgba(0, 0, 0, 0.22), transparent 72%);
  opacity: 0;
  transform: scaleX(0.35);
  transform-origin: left center;
  transition:
    opacity var(--dur-slow) var(--ease-ink),
    transform var(--dur-slow) var(--ease-ink);
  pointer-events: none;
}

.ink-button--primary:hover:not(:disabled)::after {
  opacity: 1;
  transform: scaleX(1);
}

.ink-button--primary:hover:not(:disabled) {
  box-shadow: var(--shadow-ink);
}

.ink-button--primary .label,
.ink-button--primary .dot {
  position: relative;
  z-index: 1;
}

.ink-button--primary:disabled {
  color: var(--accent-deep-ink);
  opacity: 0.45;
}

.ink-button--secondary {
  border: 1px solid var(--ink-4);
  background: color-mix(in srgb, var(--paper-1) 70%, transparent);
  color: var(--ink-2);
}

.ink-button--secondary:hover:not(:disabled) {
  background: var(--paper-3);
}

.ink-button--secondary:disabled {
  border-color: var(--ink-5);
}

.ink-button--quiet {
  color: var(--ink-3);
}

.ink-button--quiet:hover:not(:disabled) {
  background: color-mix(in srgb, var(--paper-3) 70%, transparent);
  color: var(--ink-1);
}

.ink-button--danger {
  color: var(--ink-4);
}

.ink-button--danger:hover:not(:disabled) {
  background: color-mix(in srgb, var(--cinnabar) 8%, transparent);
  color: var(--cinnabar);
}

/* loading 墨點 */
.dot {
  width: 10px;
  height: 11px;
  border-radius: 55% 45% 60% 40%;
  background: radial-gradient(circle at 42% 38%, currentcolor, transparent 75%);
  animation: dot-breathe 1.4s var(--ease-ink) infinite;
}

@keyframes dot-breathe {
  50% {
    opacity: 0.35;
  }
}
</style>

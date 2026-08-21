<script setup lang="ts">
/**
 * 狀態章。error 是朱印（微旋，全頁少數的紅），success 是青瓷淡彩，
 * 其餘走墨色——狀態的輕重直接對映墨的濃淡。
 */
const props = withDefaults(
  defineProps<{ tone?: 'neutral' | 'success' | 'error' | 'info' }>(),
  { tone: 'neutral' },
)
</script>

<template>
  <span class="ink-tag" :class="`ink-tag--${props.tone}`"><slot /></span>
</template>

<style scoped>
.ink-tag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px;
  font-size: 0.75rem;
  letter-spacing: 0.08em;
  border-radius: var(--radius-a);
  border: 1px solid var(--ink-5);
  color: var(--ink-3);
  background: color-mix(in srgb, var(--paper-1) 70%, transparent);
  white-space: nowrap;
}

.ink-tag--info {
  border-color: var(--ink-4);
  color: var(--ink-2);
}

.ink-tag--success {
  border-color: color-mix(in srgb, var(--celadon) 90%, transparent);
  background: color-mix(in srgb, var(--celadon) 14%, transparent);
  color: var(--celadon-ink);
}

.ink-tag--error {
  border-color: color-mix(in srgb, var(--cinnabar) 65%, transparent);
  background: color-mix(in srgb, var(--cinnabar) 7%, transparent);
  color: var(--cinnabar);
  letter-spacing: 0.1em;
  transform: rotate(-1.5deg);
}
</style>

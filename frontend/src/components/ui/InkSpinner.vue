<script setup lang="ts">
/** 載入中：一滴墨的呼吸，配楷體短句。 */
const props = withDefaults(defineProps<{ text?: string }>(), { text: '墨跡未乾……' })
</script>

<template>
  <div class="spinner" role="status">
    <span class="blot" aria-hidden="true"></span>
    <span class="text">{{ props.text }}</span>
  </div>
</template>

<style scoped>
.spinner {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  color: var(--ink-4);
  font-size: 0.8125rem;
}

.blot {
  width: 13px;
  height: 14px;
  border-radius: 55% 45% 60% 40%;
  background: radial-gradient(
    circle at 42% 38%,
    color-mix(in srgb, var(--ink-1) 50%, transparent),
    color-mix(in srgb, var(--ink-1) 5%, transparent) 72%
  );
  animation: blot-breathe 1.6s var(--ease-ink) infinite;
}

.text {
  font-family: var(--font-kai);
  letter-spacing: 0.1em;
}

@keyframes blot-breathe {
  50% {
    opacity: 0.35;
    transform: scale(0.9);
  }
}
</style>

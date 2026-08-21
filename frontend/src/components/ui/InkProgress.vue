<script setup lang="ts">
/** 進度條：墨由濃轉淡，右端暈開——進度即墨跡向前滲。 */
const props = defineProps<{ percent: number }>()
</script>

<template>
  <div class="track" role="progressbar" :aria-valuenow="props.percent" aria-valuemin="0" aria-valuemax="100">
    <div class="fill" :style="{ width: `${Math.min(Math.max(props.percent, 0), 100)}%` }"></div>
  </div>
</template>

<style scoped>
.track {
  height: 3px;
  background: color-mix(in srgb, var(--ink-5) 60%, transparent);
  border-radius: 2px;
}

.fill {
  height: 3px;
  border-radius: 2px;
  background: linear-gradient(
    90deg,
    var(--accent-deep),
    var(--ink-3) 70%,
    color-mix(in srgb, var(--ink-3) 20%, transparent)
  );
  transition: width var(--dur-slow) var(--ease-ink);
}
</style>

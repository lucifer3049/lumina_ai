<script setup lang="ts">
/** 筆觸分隔線（帶飛白），取代直線 <hr>。寬度由外層控制。 */
const props = withDefaults(defineProps<{ opacity?: number }>(), { opacity: 0.55 })
</script>

<template>
  <svg
    viewBox="0 0 300 9"
    class="divider"
    preserveAspectRatio="none"
    aria-hidden="true"
    :style="{ opacity: props.opacity }"
  >
    <path
      d="M2 5.5 C 46 2.2, 110 1.6, 168 3.2 C 166 4.6, 118 5.6, 58 5.2 C 28 5, 8 5.9, 2 6 Z"
      fill="var(--ink-2)"
    ></path>
    <path
      d="M188 3.4 C 232 4, 272 3.8, 298 3.4 C 296 4.4, 262 5.6, 214 5.3 C 202 5.2, 192 5.5, 189 5.2 Z"
      fill="var(--ink-2)"
      opacity="0.7"
    ></path>
  </svg>
</template>

<style scoped>
.divider {
  display: block;
  width: 100%;
  height: 9px;
}
</style>

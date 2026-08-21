<script setup lang="ts">
/** 空狀態：留白本身就是水墨的語言，只放一枝淡竹與一句話。 */
defineProps<{ description: string }>()
</script>

<template>
  <div class="empty">
    <svg viewBox="0 0 80 64" class="art" aria-hidden="true">
      <path
        d="M30 6 C 28 22, 30 40, 29 58"
        stroke="var(--ink-4)"
        stroke-width="2.6"
        stroke-linecap="round"
        fill="none"
      ></path>
      <path d="M26 34 L 34 34" stroke="var(--ink-4)" stroke-width="1.4" stroke-linecap="round"></path>
      <path d="M31 18 C 42 13, 54 11, 66 13 C 55 19, 43 21, 31 18 Z" fill="var(--ink-4)"></path>
      <path d="M30 40 C 39 44, 46 50, 50 58 C 41 53, 34 47, 30 40 Z" fill="var(--ink-4)" opacity="0.8"></path>
    </svg>
    <span class="text">{{ description }}</span>
  </div>
</template>

<style scoped>
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 44px 20px;
}

.art {
  width: 64px;
  height: 52px;
  opacity: 0.55;
}

.text {
  font-size: 0.875rem;
  color: var(--ink-3);
  text-align: center;
  line-height: 1.8;
}
</style>

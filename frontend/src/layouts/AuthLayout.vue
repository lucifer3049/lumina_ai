<script setup lang="ts">
/**
 * 登入／註冊頁的外框（03 §2）：置中一張卡，沒有側欄與頁首——
 * 未登入的人沒有可以導航去的地方。
 */
</script>

<template>
  <main class="auth-layout">
    <slot />
  </main>
</template>

<style scoped>
.auth-layout {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 1.5rem;
}
</style>

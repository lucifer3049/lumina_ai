<script setup lang="ts">
import { RouterView } from 'vue-router'
</script>

<template>
  <!-- layouts（DefaultLayout / AuthLayout / AdminLayout，03 §2）於 1E 進來，
       屆時由 route 的 meta 決定套哪一個。Phase 0 只有 RouterView。 -->
  <RouterView />
</template>

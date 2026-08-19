<script setup lang="ts">
/**
 * layout 由 route meta 決定（03 §2）：public 路由（登入）套 AuthLayout，
 * 其餘一律 DefaultLayout。AdminLayout 等 Phase 2 的管理面進來再加。
 */
import { computed } from 'vue'
import { RouterView, useRoute } from 'vue-router'

import AuthLayout from '@/layouts/AuthLayout.vue'
import DefaultLayout from '@/layouts/DefaultLayout.vue'

const route = useRoute()
const layout = computed(() => (route.meta.public === true ? AuthLayout : DefaultLayout))
</script>

<template>
  <component :is="layout">
    <RouterView />
  </component>
</template>

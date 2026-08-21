<script setup lang="ts">
/**
 * layout 由 route meta 決定（03 §2）：public 路由（登入）套 AuthLayout，
 * 其餘一律 DefaultLayout。AdminLayout 等 Phase 2 的管理面進來再加。
 *
 * ToastHost 掛在最外層一次；toast 狀態是 singleton（useToast.ts），
 * 不需要 provider——舊 naive-ui 時代「祖先必須有 NMessageProvider」的
 * 執行期地雷（1E-2）自此消失。
 */
import { computed } from 'vue'
import { RouterView, useRoute } from 'vue-router'

import ToastHost from '@/components/ui/ToastHost.vue'
import AuthLayout from '@/layouts/AuthLayout.vue'
import DefaultLayout from '@/layouts/DefaultLayout.vue'

const route = useRoute()
const layout = computed(() => (route.meta.public === true ? AuthLayout : DefaultLayout))
</script>

<template>
  <component :is="layout">
    <RouterView />
  </component>
  <ToastHost />
</template>

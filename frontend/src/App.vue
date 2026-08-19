<script setup lang="ts">
/**
 * layout 由 route meta 決定（03 §2）：public 路由（登入）套 AuthLayout，
 * 其餘一律 DefaultLayout。AdminLayout 等 Phase 2 的管理面進來再加。
 *
 * provider 放在最外層（1E-2）：naive-ui 的 `useMessage()` 需要祖先有
 * NMessageProvider，而 toast 是每個頁面回報失敗的方式——放在單一頁面裡的話，
 * 下一個要用的人會拿到一個執行期才發作的錯誤（「useMessage 必須在 provider 內」）。
 * NConfigProvider 帶 zh-TW：不給的話 naive-ui 內建文案（分頁、日期、確認鈕）是英文。
 */
import { NConfigProvider, NMessageProvider, dateZhTW, zhTW } from 'naive-ui'
import { computed } from 'vue'
import { RouterView, useRoute } from 'vue-router'

import AuthLayout from '@/layouts/AuthLayout.vue'
import DefaultLayout from '@/layouts/DefaultLayout.vue'

const route = useRoute()
const layout = computed(() => (route.meta.public === true ? AuthLayout : DefaultLayout))
</script>

<template>
  <NConfigProvider :locale="zhTW" :date-locale="dateZhTW">
    <NMessageProvider>
      <component :is="layout">
        <RouterView />
      </component>
    </NMessageProvider>
  </NConfigProvider>
</template>

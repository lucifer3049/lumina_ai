<script setup lang="ts">
/**
 * 登入頁（1E-1；09 §2.1）。
 *
 * 表單三欄對齊後端 LoginIn：tenant_slug 必填——登入發生在租戶身分存在之前，
 * RLS 之下沒有租戶就查不到任何使用者（backend/api/schemas/auth.py）。
 *
 * 這一頁刻意薄（03 §1「views 不含業務規則」）：驗證交給後端、狀態交給
 * store，這裡只做展示與轉場。單元測試因此不設（03 §6.1 元件層選配），
 * 真驗證是 1E-4 Playwright 的第一步。
 */
import { NAlert, NButton, NCard, NForm, NFormItem, NInput } from 'naive-ui'
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const tenantSlug = ref('')
const email = ref('')
const password = ref('')
const submitting = ref(false)
const errorMessage = ref<string | null>(null)

/** guard 塞進來的原目的地；只接站內路徑，擋 open-redirect。 */
function redirectTarget(): string {
  const target = route.query.redirect
  return typeof target === 'string' && target.startsWith('/') ? target : '/'
}

async function onSubmit(): Promise<void> {
  if (submitting.value) {
    return
  }
  submitting.value = true
  errorMessage.value = null
  try {
    await auth.login({
      tenantSlug: tenantSlug.value.trim(),
      email: email.value.trim(),
      password: password.value,
    })
    await router.replace(redirectTarget())
  } catch (error) {
    if (error instanceof ApiError && error.code === 'AUTH_INVALID_CREDENTIALS') {
      // 刻意不區分「哪一欄錯」：後端也不區分（防帳號枚舉），前端不自己編故事。
      errorMessage.value = '登入失敗：租戶代號、Email 或密碼不正確'
    } else if (error instanceof ApiError) {
      errorMessage.value = error.detail
    } else {
      errorMessage.value = '登入失敗，請稍後重試'
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <NCard title="登入 Lumina AI" class="login-card">
    <NForm label-placement="top" @submit.prevent="onSubmit">
      <NFormItem label="租戶代號">
        <NInput v-model:value="tenantSlug" placeholder="例如 acme" :disabled="submitting" />
      </NFormItem>
      <NFormItem label="Email">
        <NInput v-model:value="email" placeholder="you@example.com" :disabled="submitting" />
      </NFormItem>
      <NFormItem label="密碼">
        <NInput
          v-model:value="password"
          type="password"
          show-password-on="click"
          :disabled="submitting"
          @keyup.enter="onSubmit"
        />
      </NFormItem>
      <NAlert v-if="errorMessage" type="error" class="login-error" :show-icon="true">
        {{ errorMessage }}
      </NAlert>
      <NButton type="primary" block attr-type="submit" :loading="submitting"> 登入 </NButton>
    </NForm>
  </NCard>
</template>

<style scoped>
.login-card {
  width: 100%;
  max-width: 22rem;
}

.login-error {
  margin-bottom: 16px;
}
</style>

<script setup lang="ts">
/**
 * 登入頁（1E-1；09 §2.1）。
 *
 * 表單三欄對齊後端 LoginIn：tenant_slug 必填——登入發生在租戶身分存在之前，
 * RLS 之下沒有租戶就查不到任何使用者（backend/api/schemas/auth.py）。
 *
 * 這一頁刻意薄（03 §1「views 不含業務規則」）：驗證交給後端、狀態交給
 * store，這裡只做展示與轉場。登入面用書畫裝裱式雙線框（設計語言）。
 */
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import InkButton from '@/components/ui/InkButton.vue'
import InkInput from '@/components/ui/InkInput.vue'
import SealMark from '@/components/ui/SealMark.vue'
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
  <div class="login">
    <div class="brand">
      <SealMark char="智" :size="40" />
      <div class="brand-name">Lumina AI</div>
      <div class="brand-tagline">智啟千年 · 知識如水</div>
    </div>

    <div class="panel">
      <form class="panel-inner" @submit.prevent="onSubmit">
        <div class="panel-title">登入</div>

        <div class="field">
          <label class="label" for="login-tenant">租戶代號</label>
          <InkInput
            id="login-tenant"
            v-model="tenantSlug"
            variant="underline"
            placeholder="例如 acme"
            :disabled="submitting"
          />
        </div>

        <div class="field">
          <label class="label" for="login-email">Email</label>
          <InkInput
            id="login-email"
            v-model="email"
            variant="underline"
            placeholder="you@example.com"
            :disabled="submitting"
          />
        </div>

        <div class="field">
          <label class="label" for="login-password">密碼</label>
          <InkInput
            id="login-password"
            v-model="password"
            type="password"
            variant="underline"
            :disabled="submitting"
            @keyup.enter="onSubmit"
          />
        </div>

        <p v-if="errorMessage" class="error" role="alert">{{ errorMessage }}</p>

        <!-- hover 時「入卷」小印浮現——落款只在筆將落紙的那一刻出現 -->
        <div class="submit-row">
          <InkButton variant="primary" class="submit" attr-type="submit" :loading="submitting">
            登入
          </InkButton>
          <SealMark char="入卷" :size="34" class="seal-hint" />
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.login {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 26px;
  width: 100%;
  max-width: 24rem;
}

.brand {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}

.brand-name {
  font-family: var(--font-serif);
  font-size: 1.9375rem;
  font-weight: 900;
  letter-spacing: 0.09em;
  color: var(--ink-1);
}

.brand-tagline {
  font-family: var(--font-kai);
  font-size: 0.875rem;
  letter-spacing: 0.3em;
  text-indent: 0.3em;
  color: var(--ink-3);
}

/* 書畫裝裱式雙線框：半透明宣紙，浮在雲霧與山水之間 */
.panel {
  width: 100%;
  border: 1px solid var(--ink-5);
  padding: 5px;
  background: color-mix(in srgb, var(--paper-2) 82%, transparent);
  box-sizing: border-box;
}

.panel-inner {
  border: 1px solid var(--ink-5);
  padding: 26px 32px 32px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.panel-title {
  font-family: var(--font-serif);
  font-size: 1.25rem;
  font-weight: 700;
  letter-spacing: 0.2em;
  color: var(--ink-1);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.label {
  font-size: 0.8125rem;
  font-weight: 500;
  letter-spacing: 0.1em;
  color: var(--ink-2);
}

.error {
  margin: 0;
  padding: 9px 13px;
  font-size: 0.8125rem;
  line-height: 1.7;
  color: var(--cinnabar);
  border: 1px solid color-mix(in srgb, var(--cinnabar) 50%, transparent);
  border-radius: var(--radius-a);
  background: color-mix(in srgb, var(--cinnabar) 6%, transparent);
}

.submit-row {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: 4px;
}

.submit {
  flex: 1;
}

.seal-hint {
  opacity: 0;
  transition: opacity var(--dur-slow) var(--ease-ink);
}

.submit-row:hover .seal-hint,
.submit-row:focus-within .seal-hint {
  opacity: 1;
}
</style>

<script setup lang="ts">
/**
 * 輸入框：宣紙底、清墨細框。`type="textarea"` 時渲染 textarea 並隨內容長高
 * （聊天輸入需要）；`type="password"` 附「按著看」的顯示切換，對齊舊登入頁
 * 的 show-password-on click 行為。
 *
 * 事件（keydown 等）經 $attrs 落在原生元素上，呼叫端照 Vue 慣例綁即可。
 */
import { computed, ref, useAttrs } from 'vue'

defineOptions({ inheritAttrs: false })

const props = withDefaults(
  defineProps<{
    modelValue: string
    type?: 'text' | 'password' | 'textarea'
    /** underline＝宣紙上的一條墨底線，focus 時筆畫由左向右暈開（登入頁用）。 */
    variant?: 'box' | 'underline'
    placeholder?: string
    disabled?: boolean
    maxlength?: number
    /** textarea 的最小列數。 */
    rows?: number
  }>(),
  { type: 'text', variant: 'box', rows: 2 },
)

const emit = defineEmits<{ (event: 'update:modelValue', value: string): void }>()

const attrs = useAttrs()
const revealed = ref(false)
const inputType = computed(() => {
  if (props.type === 'password') {
    return revealed.value ? 'text' : 'password'
  }
  return props.type
})

function onInput(event: Event): void {
  const element = event.target as HTMLInputElement | HTMLTextAreaElement
  emit('update:modelValue', element.value)
  if (props.type === 'textarea') {
    element.style.height = 'auto'
    element.style.height = `${Math.min(element.scrollHeight, 200)}px`
  }
}
</script>

<template>
  <textarea
    v-if="props.type === 'textarea'"
    class="ink-input ink-input--textarea"
    :value="props.modelValue"
    :placeholder="props.placeholder"
    :disabled="props.disabled"
    :maxlength="props.maxlength"
    :rows="props.rows"
    v-bind="attrs"
    @input="onInput"
  ></textarea>
  <div v-else class="wrap" :class="{ 'wrap--underline': props.variant === 'underline' }">
    <input
      class="ink-input"
      :class="{ 'ink-input--underline': props.variant === 'underline' }"
      :type="inputType"
      :value="props.modelValue"
      :placeholder="props.placeholder"
      :disabled="props.disabled"
      :maxlength="props.maxlength"
      v-bind="attrs"
      @input="onInput"
    />
    <button
      v-if="props.type === 'password'"
      type="button"
      class="reveal"
      :aria-label="revealed ? '隱藏密碼' : '顯示密碼'"
      @click="revealed = !revealed"
    >
      <svg
        width="18"
        height="18"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.6"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path
          d="M2 12 C 5 6.5, 9 4.5, 12 4.5 C 15 4.5, 19 6.5, 22 12 C 19 17.5, 15 19.5, 12 19.5 C 9 19.5, 5 17.5, 2 12 Z"
        ></path>
        <circle cx="12" cy="12" r="3"></circle>
        <path v-if="revealed" d="M4 20 L 20 4"></path>
      </svg>
    </button>
  </div>
</template>

<style scoped>
.wrap {
  position: relative;
  display: flex;
  width: 100%;
}

.ink-input {
  width: 100%;
  box-sizing: border-box;
  min-height: 44px;
  padding: 10px 13px;
  font-family: var(--font-body);
  font-size: 0.875rem;
  line-height: 1.6;
  color: var(--ink-1);
  background: color-mix(in srgb, var(--paper-1) 85%, transparent);
  border: 1px solid var(--ink-5);
  border-radius: var(--radius-a);
  transition: border-color var(--dur-med) var(--ease-ink);
}

.ink-input::placeholder {
  color: var(--ink-4);
}

.ink-input:focus {
  outline: none;
  border-color: var(--ink-3);
}

.ink-input:disabled {
  color: var(--ink-4);
  background: var(--paper-3);
}

.ink-input--textarea {
  resize: none;
}

/* underline：像寫在宣紙上，只有一條淡墨底線 */
.ink-input--underline {
  background: transparent;
  border: none;
  border-bottom: 1px solid var(--ink-5);
  border-radius: 0;
  padding-left: 2px;
  padding-right: 2px;
}

.ink-input--underline:focus {
  border-bottom-color: var(--ink-5);
}

/* focus 的筆畫：由左向右慢慢劃過 */
.wrap--underline::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 2px;
  background: linear-gradient(90deg, var(--ink-1), var(--ink-3) 80%, rgba(110, 101, 91, 0.3));
  transform: scaleX(0);
  transform-origin: left;
  transition: transform var(--dur-slow) var(--ease-ink);
  pointer-events: none;
}

.wrap--underline:focus-within::after {
  transform: scaleX(1);
}

.reveal {
  position: absolute;
  top: 0;
  right: 4px;
  bottom: 0;
  display: flex;
  align-items: center;
  padding: 0 9px;
  border: none;
  background: transparent;
  color: var(--ink-4);
  cursor: pointer;
}

.reveal:hover {
  color: var(--ink-2);
}
</style>

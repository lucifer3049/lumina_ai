/**
 * Toast（取代 naive-ui 的 useMessage）。
 *
 * 狀態放在 module scope 而不是 provide/inject：useMessage 的「祖先必須有
 * provider」曾是 1E-2 的執行期地雷（App.vue 舊註解），singleton 讓任何 view
 * 直接呼叫就能用，唯一的代價是全 app 共用一條佇列——而 toast 本來就該如此。
 */
import { readonly, ref } from 'vue'

export type ToastType = 'success' | 'error' | 'warning' | 'info'

export interface ToastItem {
  id: number
  type: ToastType
  text: string
}

const items = ref<ToastItem[]>([])
let nextId = 1

function push(type: ToastType, text: string): void {
  const id = nextId
  nextId += 1
  items.value = [...items.value, { id, type, text }]
  // 錯誤多留一會兒：使用者需要時間讀原因；成功只是確認，短些。
  const ttlMs = type === 'error' ? 6000 : 3500
  setTimeout(() => {
    dismiss(id)
  }, ttlMs)
}

export function dismiss(id: number): void {
  items.value = items.value.filter((item) => item.id !== id)
}

/** ToastHost 渲染用；views 不要直接碰。 */
export const toastItems = readonly(items)

export function useToast(): Record<ToastType, (text: string) => void> {
  return {
    success: (text) => push('success', text),
    error: (text) => push('error', text),
    warning: (text) => push('warning', text),
    info: (text) => push('info', text),
  }
}

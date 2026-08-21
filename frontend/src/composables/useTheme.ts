/**
 * 主題（晝／夜）切換。singleton：狀態在 module scope，任何元件呼叫即用。
 *
 * 首屏不閃白/閃黑：index.html 有一段 inline script 在 app 掛載前就把
 * localStorage 的偏好寫上 <html data-theme>；這裡初始化時讀同一個 key，
 * 兩邊的預設都是 light（宣紙是這套設計語言的「原色」）。
 */
import { readonly, ref } from 'vue'

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'lumina.theme'

const theme = ref<Theme>('light')

function apply(next: Theme): void {
  theme.value = next
  if (typeof document !== 'undefined') {
    if (next === 'dark') {
      document.documentElement.setAttribute('data-theme', 'dark')
    } else {
      document.documentElement.removeAttribute('data-theme')
    }
  }
  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch {
    // 隱私模式等拿不到 storage：主題仍生效，只是不記住。
  }
}

// module 載入時同步一次（inline script 可能已經設好 attribute）。
if (typeof window !== 'undefined') {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'dark') {
      apply('dark')
    }
  } catch {
    // 同上。
  }
}

export function useTheme(): {
  theme: Readonly<typeof theme>
  toggle: () => void
  set: (next: Theme) => void
} {
  return {
    theme: readonly(theme),
    toggle: () => apply(theme.value === 'dark' ? 'light' : 'dark'),
    set: apply,
  }
}

/**
 * Vite / Vitest 設定（03 §2）。
 *
 * 兩者共用一份：`defineConfig` 取自 `vitest/config`，測試才會沿用同一組 alias 與
 * plugin。分成兩個檔案的話，`@/` 這類 alias 必然有一天只改到一邊，症狀是
 * 「程式跑得動、測試說找不到模組」。
 */
import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    // dev proxy → FastAPI。沒有它時瀏覽器對 5173 發請求會吃 CORS 錯誤，而那個
    // 錯誤訊息完全不會提到 proxy，排查方向常被帶偏。
    // 目標可用 VITE_DEV_API_TARGET 覆寫（例如後端跑在容器裡、或改了埠）。
    proxy: {
      '/api': {
        target: process.env.VITE_DEV_API_TARGET ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    // node 環境即可：本階段的測試對象是 fetch wrapper 與設定檔，沒有 DOM。
    // 元件測試（03 §6.1 的 component 層）進來時再視需要切 jsdom。
    environment: 'node',
    include: ['tests/**/*.spec.ts'],
    // 型別層測試（tests/types/*.test-d.ts）跟著 `vitest run` 一起跑。
    // 分成另一個指令的話，CI 少跑那一個不會有任何徵兆——generated 產出空殼時
    // 檔案層斷言全部照樣通過（見 tests/unit/codegen.spec.ts 的說明）。
    typecheck: {
      enabled: true,
      include: ['tests/types/**/*.test-d.ts'],
      // 必須是 vue-tsc 而非預設的 tsc：tsc 不認得 `.vue`，會把 main.ts 的
      // `import App from './App.vue'` 報成「找不到模組」——錯誤出現在型別測試的
      // 結果裡，卻與被測的型別毫無關係。
      checker: 'vue-tsc',
    },
    // 測試不讀 .env（那會讓結果隨本機設定而變），baseURL 在此固定。
    // 值刻意用一個不存在的網域：msw 沒攔到的請求會立刻失敗，不會打到真實服務。
    env: {
      VITE_API_BASE_URL: 'http://api.test',
    },
  },
})

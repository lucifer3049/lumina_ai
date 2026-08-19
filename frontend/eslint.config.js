/**
 * ESLint flat config（eslint 9）。
 *
 * 直接用 typescript-eslint 而非 @vue/eslint-config-typescript：後者的匯出介面在
 * 大版本之間換過（`vueTsEslintConfig()` → `defineConfigWithVueTs`），升版時整份
 * 設定會直接壞掉，而錯誤訊息落在 config 載入階段，看起來像 eslint 自己壞了。
 *
 * eslint 不做型別推導——TS strict 的守門是 `pnpm typecheck`（vue-tsc）。
 * 兩者其中一個被拿掉時另一個照樣全綠，所以 CI 兩條都跑。
 */
import js from '@eslint/js'
import prettier from 'eslint-config-prettier'
import pluginVue from 'eslint-plugin-vue'
import ts from 'typescript-eslint'

export default ts.config(
  {
    // 產物不受 lint 管：規則會要求改寫產物，於是每次 codegen 之後 lint 都紅，
    // 或更糟——有人「順手修好」產物，下次重新產生就被蓋掉，而 CI 的漂移檢查
    // 會在一個完全無關的 PR 上爆掉（03 §7）。
    ignores: ['dist/**', 'src/api/generated/**'],
  },
  js.configs.recommended,
  ...ts.configs.recommended,
  ...pluginVue.configs['flat/essential'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: { parser: ts.parser },
    },
    rules: {
      // `no-undef` 在 TS 檔由 typescript-eslint 的預設關掉（它會把 File、fetch 這類
      // 瀏覽器全域報成未定義，而那是 TS 的 lib 在管的事）。`.vue` 的 <script setup>
      // 同樣是 TS，卻不在那個關閉範圍內——不關的話，用到任何瀏覽器 API 的元件都會
      // 紅，而真正的守門（vue-tsc）早就驗過它們存在。
      'no-undef': 'off',
    },
  },
  // 放最後：關掉所有與格式有關的規則，格式一律交給 prettier。
  // 兩邊都管格式時會互相打架，而症狀是 `--fix` 之後 lint 仍然紅。
  prettier,
)

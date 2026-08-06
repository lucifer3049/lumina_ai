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
  },
  // 放最後：關掉所有與格式有關的規則，格式一律交給 prettier。
  // 兩邊都管格式時會互相打架，而症狀是 `--fix` 之後 lint 仍然紅。
  prettier,
)

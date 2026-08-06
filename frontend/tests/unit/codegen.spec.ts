/**
 * 驗收：generated client 確實由 codegen 產出，且沒有被手改（03 §2、§7）。
 *
 * 「禁止手改」在 03 §7 是靠兩件事保證的：CI 的 diff check（`make openapi-check`，
 * 由 backend/tests/unit/test_ci_pipeline.py 釘住它存在），以及這裡——目錄真的是
 * 產物、不是有人手寫的一份 types。
 *
 * 只驗檔案層的事實，型別層的驗收在 tests/types/models.test-d.ts。
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const ROOT = fileURLToPath(new URL('../../', import.meta.url))
const GENERATED = `${ROOT}src/api/generated`

describe('src/api/generated', () => {
  it('exists and is not empty', () => {
    expect(existsSync(GENERATED), 'src/api/generated 不存在——請跑 `make gen-api`').toBe(true)
    expect(readdirSync(GENERATED).length).toBeGreaterThan(0)
  })

  it('carries the generator banner', () => {
    // openapi-typescript 產出的檔頭帶「這是自動產生的、不要改」字樣。
    // 沒有它就代表這份不是產物——可能是有人把 generated 刪掉後手寫回來，
    // 而那正是 03 §7 列為風險的情況（CI diff check 之後會擋，但錯誤訊息很難懂）。
    const files = readdirSync(GENERATED).filter((name) => name.endsWith('.ts'))
    expect(files.length, 'generated 目錄裡沒有 .ts 檔').toBeGreaterThan(0)

    for (const file of files) {
      const head = readFileSync(`${GENERATED}/${file}`, 'utf-8').slice(0, 400)
      expect(head.toLowerCase(), `${file} 沒有 codegen 的檔頭標記`).toContain('do not make direct changes')
    }
  })
})

describe('lint / format 不碰 generated', () => {
  const readIfExists = (relative: string): string =>
    existsSync(`${ROOT}${relative}`) ? readFileSync(`${ROOT}${relative}`, 'utf-8') : ''

  it('eslint ignores the generated directory', () => {
    // 產物照 lint 規則改寫 = 每次 codegen 之後 lint 都紅，或更糟：有人「順手修好」
    // 產物，下次重新產生就被蓋掉，且 CI 的 diff check 會在無關的 PR 上爆掉。
    const config = readIfExists('eslint.config.js') + readIfExists('eslint.config.ts')
    expect(config, '找不到 eslint 設定').not.toBe('')
    expect(config).toContain('src/api/generated')
  })

  it('prettier ignores the generated directory', () => {
    const ignore = readIfExists('.prettierignore')
    expect(ignore, '缺少 .prettierignore').toContain('src/api/generated')
  })
})

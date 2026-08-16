/**
 * 驗收：前端工具鏈符合 ADR-007 與 03 §6 的硬性規定。
 *
 * 這些是設定檔的內容，不是程式行為——但每一條被改掉時都**沒有任何症狀**：
 * strict 關掉後程式照跑、Node 版本沒釘時本機跑得動而 CI 不一定、dev proxy 沒了
 * 只有在真的打後端 API 時才發現。設定漂移只能用測試釘住。
 *
 * 讀檔而非 import 設定：vite.config.ts 是 ESM + 型別，直接 import 會把整個
 * vite 帶進測試行程；這裡要驗的只是「宣告了什麼」。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const ROOT = fileURLToPath(new URL('../../', import.meta.url))

const read = (relative: string): string => readFileSync(`${ROOT}${relative}`, 'utf-8')

// package.json 走嚴格 JSON——它必須是嚴格 JSON，npm/pnpm 的讀取器不接受註解，
// 因此這裡的 JSON.parse 本身就是一條斷言。
const readJson = (relative: string): Record<string, unknown> =>
  JSON.parse(read(relative)) as Record<string, unknown>

// tsconfig **不是** JSON 而是 JSONC：TypeScript 官方接受註解與尾逗號，vue-tsc、
// vite、eslint 都用 ts 自己的解析器讀它。這裡若改用 JSON.parse，一則寫在 tsconfig
// 裡的說明註解就會讓這條測試爆掉，而 typecheck / build / lint 全部照樣全綠——
// 症狀完全不指向真因（2026-08-17 CI 前端 job 全紅即是此事）。
// 用 ts.parseConfigFileTextToJson 而不是自己剝註解：字串裡的 `//`（例如 URL）
// 會讓手寫的剝除器切錯位置，而那種錯誤只在特定內容下出現。
const readTsconfig = (relative: string): Record<string, unknown> => {
  const parsed = ts.parseConfigFileTextToJson(relative, read(relative))
  expect(parsed.error, `${relative} 不是合法的 tsconfig`).toBeUndefined()
  return (parsed.config ?? {}) as Record<string, unknown>
}

describe('package.json', () => {
  const pkg = readJson('package.json')
  const scripts = (pkg.scripts ?? {}) as Record<string, string>

  // Makefile 與 CI 呼叫的是這些名字（backend/tests/unit/test_ci_pipeline.py 沿鏈追到
  // 這裡）。改名等於改掉 CI 的階段，必須是明示的動作。
  it.each(['dev', 'build', 'lint', 'typecheck', 'test', 'gen:api'])(
    'have script `%s`',
    (name) => {
      expect(scripts[name], `package.json 缺少 script \`${name}\``).toBeTruthy()
    },
  )

  it('pins Node 22 LTS (ADR-007)', () => {
    const engines = (pkg.engines ?? {}) as Record<string, string>
    // 只寫 ">=22" 而不寫上界是刻意的：ADR-007 訂的是 22 LTS 這條線，
    // 本機裝 22.x 的哪個 patch 不該讓 install 失敗。
    expect(engines.node, 'package.json 未宣告 engines.node').toMatch(/22/)
  })

  it('pins the pnpm version', () => {
    // corepack 讀這個欄位決定用哪個 pnpm。沒釘的話每個人（與 CI）用自己那版，
    // lockfile 格式在 pnpm 大版本之間會變，症狀是 --frozen-lockfile 無故失敗。
    expect(pkg.packageManager, 'package.json 未釘 packageManager').toMatch(/^pnpm@\d+\.\d+\.\d+/)
  })

  it('generates the API client from the committed contract', () => {
    // 契約檔在 repo 根（見 backend/tests/unit/test_openapi_export.py）。
    // 若這裡改成打 http://localhost:8000/openapi.json，CI 就得先起一套 API，
    // 且失去可 review 的契約 diff（09 §4）。
    expect(scripts['gen:api']).toContain('openapi-typescript')
    expect(scripts['gen:api']).toContain('openapi.json')
    expect(scripts['gen:api']).toContain('src/api/generated')
  })
})

describe('tsconfig', () => {
  // vue 的 scaffold 會拆成 tsconfig.json（references）+ tsconfig.app.json 等多份，
  // 因此掃全部：只要有一份開了、且沒有任何一份把它關掉即可。
  const configs = readdirSync(ROOT)
    .filter((name) => name.startsWith('tsconfig') && name.endsWith('.json'))
    .map((name) => ({
      name,
      options: (readTsconfig(name).compilerOptions ?? {}) as Record<string, unknown>,
    }))

  it.each(['strict', 'noUncheckedIndexedAccess'])('enable `%s` (03 §6)', (flag) => {
    expect(configs.length, '找不到任何 tsconfig*.json').toBeGreaterThan(0)
    expect(
      configs.some((config) => config.options[flag] === true),
      `沒有任何 tsconfig 開啟 ${flag}`,
    ).toBe(true)
    expect(
      configs.filter((config) => config.options[flag] === false).map((config) => config.name),
      `以下 tsconfig 把 ${flag} 關掉了`,
    ).toEqual([])
  })
})

describe('vite config', () => {
  const config = read('vite.config.ts')

  it('proxies API calls to FastAPI in dev (03 §2)', () => {
    // 沒有 proxy 時瀏覽器會對 5173 發請求並吃 CORS 錯誤；那個錯誤訊息不會提到
    // proxy，排查方向常被帶偏。
    expect(config).toContain('proxy')
    expect(config).toMatch(/['"]\/api['"]/)
  })
})

describe('.env.example', () => {
  it('documents VITE_API_BASE_URL', () => {
    // 前端的 .env 與 repo 根那份是不同的東西（Vite 只讀 frontend/ 底下、且只暴露
    // VITE_ 前綴的變數）。沒有樣板時新人不會知道要設什麼。
    expect(existsSync(`${ROOT}.env.example`), 'frontend/.env.example 不存在').toBe(true)
    expect(read('.env.example')).toContain('VITE_API_BASE_URL')
  })

  it('never carries secrets', () => {
    // Vite 會把 VITE_ 開頭的變數**編譯進 bundle**，任何人打開 devtools 都看得到
    // （CLAUDE.md 鐵則 9：secrets 不進前端）。
    expect(read('.env.example')).not.toMatch(/SECRET|PASSWORD|TOKEN|API_KEY/i)
  })
})

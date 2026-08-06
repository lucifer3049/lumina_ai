/**
 * 驗收：codegen 產出的型別真的可用，且 types/models.ts 是唯一 import 入口（03 §2）。
 *
 * generated 目錄裡全是型別，執行期沒有任何東西——所以 runtime 測試驗不到它。
 * 這支跑在 vitest 的 typecheck 模式（`vitest run --typecheck`，設定於 vitest.config.ts），
 * 型別對不上就是紅燈。
 *
 * 為什麼要有這層：codegen 若默默產出空殼（schema 壞掉、路徑寫錯），檔案照樣存在、
 * banner 也在、lint 也過——codegen.spec.ts 那些檔案層斷言全部會通過。
 */
import { assertType, describe, expectTypeOf, it } from 'vitest'

import type { ProblemDetail, components, paths } from '@/types/models'

describe('generated 型別經 types/models.ts 對外', () => {
  it('re-exports the OpenAPI root types', () => {
    // 元件必須是**具名**的：`paths` / `components` 若是 any（generated 壞掉時常見），
    // 下面的斷言不會失敗，所以額外檢查它們不是 any。
    expectTypeOf<paths>().not.toBeAny()
    expectTypeOf<components>().not.toBeAny()
  })

  it('exposes ProblemDetail with the fields client.ts relies on (09 §1.3)', () => {
    // 必填欄位以 api/schemas/problem.py 為準：type / title / status / detail /
    // request_id 必有，code 等 extension member 才是選填。少列一個就過不了型別檢查，
    // 這正是要的——契約的必填性在這裡被釘住。
    assertType<ProblemDetail>({
      type: '/errors/resource-not-found',
      title: 'Resource not found',
      status: 404,
      detail: '找不到指定的資源',
      request_id: 'abc123',
    })

    expectTypeOf<ProblemDetail>().toHaveProperty('status')
    expectTypeOf<ProblemDetail['status']>().toEqualTypeOf<number>()
  })
})

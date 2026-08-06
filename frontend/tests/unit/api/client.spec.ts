/**
 * 驗收：api/client.ts 的傳輸層契約（03 §2、09 §1.3、11 §4.1）。
 *
 * client.ts 是前端唯一碰網路的地方，它的職責在 Phase 0 只有四件：
 * 接 baseURL、逾時、把後端的 Problem Details 正規化成一種例外、以及**永遠不吞錯**。
 * 401 refresh（03 §3.3）屬於 1A 的認證工作包，本階段刻意不做。
 *
 * 用 msw 攔在網路層而不是 stub 掉 fetch（03 §6.1 的慣例）：stub fetch 等於把
 * 「client 怎麼呼叫 fetch」寫死進測試，之後改用別的傳輸方式時測試會全紅但行為沒變。
 */
import { HttpResponse, http, delay } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'

import { API_TIMEOUT_MS, ApiError, request } from '@/api/client'

const BASE_URL = 'http://api.test'
const server = setupServer()

beforeAll(() => {
  // onUnhandledRequest: 'error'——測試打到沒被攔的網址時要立刻失敗。
  // 預設是放行，那會讓一條寫錯路徑的測試安靜地打到真實網路。
  server.listen({ onUnhandledRequest: 'error' })
})
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

describe('成功路徑', () => {
  it('prefixes the base URL and returns parsed JSON', async () => {
    server.use(http.get(`${BASE_URL}/api/v1/things`, () => HttpResponse.json({ items: [1] })))

    await expect(request<{ items: number[] }>('/api/v1/things')).resolves.toEqual({ items: [1] })
  })

  it('handles 204 without a body', async () => {
    // JSON.parse('') 會丟 SyntaxError，且那個錯誤不是 ApiError——呼叫端的
    // catch (e: ApiError) 就接不住，變成未處理的例外。
    server.use(http.delete(`${BASE_URL}/api/v1/things/1`, () => new HttpResponse(null, { status: 204 })))

    await expect(request('/api/v1/things/1', { method: 'DELETE' })).resolves.toBeNull()
  })
})

describe('Problem Details 正規化（09 §1.3）', () => {
  it('maps application/problem+json to ApiError', async () => {
    server.use(
      http.get(`${BASE_URL}/api/v1/things/404`, () =>
        HttpResponse.json(
          {
            type: '/errors/resource-not-found',
            title: 'Resource not found',
            status: 404,
            detail: '找不到指定的資源',
            code: 'RESOURCE_NOT_FOUND',
            request_id: 'abc123',
          },
          { status: 404, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )

    const error = await request('/api/v1/things/404').catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({
      status: 404,
      // code 是 client 唯一該拿來做判斷的穩定值（09 附錄 A）。
      code: 'RESOURCE_NOT_FOUND',
      detail: '找不到指定的資源',
      requestId: 'abc123',
    })
  })

  it('keeps `code` null when the server did not give one', async () => {
    // 405 這類走 about:blank 的回應沒有 code（見 api/main.py 的 problem_response）。
    // 前端不可以從 status 反推一個看起來像 code 的字串——那會讓 UI 開始判斷
    // 一個後端契約裡不存在的值。
    server.use(
      http.get(`${BASE_URL}/api/v1/things`, () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Method Not Allowed', status: 405, detail: '不允許' },
          { status: 405, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )

    const error = (await request('/api/v1/things').catch((e: unknown) => e)) as ApiError

    expect(error.code).toBeNull()
    expect(error.status).toBe(405)
  })

  it('falls back to the X-Request-Id header', async () => {
    // 錯誤回應永遠帶這個標頭（api/main.py 的 request_id_middleware）。body 解不出來時
    // 它是使用者回報問題後唯一能追查的線索，不能掉。
    server.use(
      http.get(`${BASE_URL}/api/v1/things`, () =>
        new HttpResponse('<html>502 Bad Gateway</html>', {
          status: 502,
          headers: { 'Content-Type': 'text/html', 'X-Request-Id': 'from-header' },
        }),
      ),
    )

    const error = (await request('/api/v1/things').catch((e: unknown) => e)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(502)
    expect(error.requestId).toBe('from-header')
    // 反向代理吐的 HTML 不可以原樣進 UI（既難讀也可能夾帶內部資訊）。
    expect(error.detail).not.toContain('<html>')
  })
})

describe('client 端失敗（不是後端契約的一部分）', () => {
  it('reports network failures as ApiError', async () => {
    server.use(http.get(`${BASE_URL}/api/v1/things`, () => HttpResponse.error()))

    const error = (await request('/api/v1/things').catch((e: unknown) => e)) as ApiError

    // 呼叫端只需要 catch 一種例外。放任 TypeError: Failed to fetch 冒出去，
    // 每個呼叫點都得自己判斷例外型別。
    expect(error).toBeInstanceOf(ApiError)
    expect(error.code).toBe('NETWORK_ERROR')
    expect(error.status).toBe(0)
  })

  it('defaults to the 15s HTTP budget (11 §4.1 Timeout 全域字典)', () => {
    // 值取自後端同一張表的「HTTP 對外 15s」。改這個數字要連同 11 §4.1 一起改，
    // 否則前後端對「多久算逾時」的認知會分岔。
    expect(API_TIMEOUT_MS).toBe(15_000)
  })

  it('aborts the request on timeout (11 §4.1：所有外呼必有 timeout)', async () => {
    let aborted = false
    server.use(
      http.get(`${BASE_URL}/api/v1/slow`, async ({ request: incoming }) => {
        incoming.signal.addEventListener('abort', () => {
          aborted = true
        })
        await delay(200)
        return HttpResponse.json({})
      }),
    )

    const error = (await request('/api/v1/slow', { timeoutMs: 20 }).catch(
      (e: unknown) => e,
    )) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.code).toBe('TIMEOUT')
    // 只 reject 不中止的話，連線會留著直到伺服器回應——分頁切換頻繁時會累積。
    expect(aborted, '逾時後沒有真的中止請求').toBe(true)
  })
})

/**
 * 驗收：composables/usePolling.ts（1E-2；03 §2、08 §5 進度回報）。
 *
 * ETL 進度沒有推播管道——文件狀態只能問（`GET /documents/{id}` 與列表）。輪詢
 * 因此是 1E-2 的必需品，而它有四個一旦漏掉就會在正式環境才發作的性質：
 *
 * 1. **不重疊**：用 setInterval 的話，後端慢於間隔時請求會疊起來，越疊越慢。
 * 2. **停得掉**：離開頁面後還在打的輪詢是看不見的漏水，且 401 之後會不斷觸發 refresh。
 * 3. **失敗要退讓**：後端掛掉時每 3 秒重試一次是在幫倒忙，且錯誤 toast 會洗版。
 * 4. **失敗夠多次就放棄**：無限重試的頁面永遠不會告訴使用者「壞了」。
 *
 * 用 vitest 的 fake timers：真的等 3 秒的測試會讓整組測試慢到沒人想跑。
 */
import { effectScope } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { usePolling } from '@/composables/usePolling'

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

/** 在一個 effect scope 裡建立 polling，回傳它與「模擬元件卸載」的函式。 */
function inScope<T>(build: () => T): { value: T; dispose: () => void } {
  const scope = effectScope()
  const value = scope.run(build) as T
  return { value, dispose: () => scope.stop() }
}

describe('節奏', () => {
  it('does not fire immediately — the caller has just fetched', async () => {
    // start() 時立刻打一次，等於每次進頁面都送兩個一樣的請求（頁面自己已經載過）。
    const fn = vi.fn().mockResolvedValue(undefined)
    const { value: polling } = inScope(() => usePolling(fn, { intervalMs: 1000 }))

    polling.start()
    expect(fn).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1000)
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('waits for the previous run to settle before scheduling the next', async () => {
    // 這一條是 setInterval 與「跑完再排」的分水嶺：fn 比間隔慢時，
    // setInterval 會讓請求疊起來（第 2 次在第 1 次還沒回來就出發）。
    let resolveFirst: (() => void) | undefined
    const fn = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<void>((resolve) => {
            resolveFirst = resolve
          }),
      )
      .mockResolvedValue(undefined)
    const { value: polling } = inScope(() => usePolling(fn, { intervalMs: 1000 }))

    polling.start()
    await vi.advanceTimersByTimeAsync(1000)
    expect(fn).toHaveBeenCalledTimes(1)

    // 第一次還沒 resolve——時間再過三個間隔也不准有第二次。
    await vi.advanceTimersByTimeAsync(3000)
    expect(fn).toHaveBeenCalledTimes(1)

    resolveFirst?.()
    await vi.advanceTimersByTimeAsync(1000)
    expect(fn).toHaveBeenCalledTimes(2)
  })

  it('reports whether it is running', () => {
    const { value: polling } = inScope(() =>
      usePolling(vi.fn().mockResolvedValue(undefined), { intervalMs: 1000 }),
    )

    expect(polling.isPolling.value).toBe(false)
    polling.start()
    expect(polling.isPolling.value).toBe(true)
    polling.stop()
    expect(polling.isPolling.value).toBe(false)
  })

  it('ignores a second start() while already running', async () => {
    // 兩個 watcher 同時觸發 start 是常態（狀態變了、路由參數也變了）；
    // 沒有這條的話會有兩條計時器同時跑，頻率變兩倍而且停不乾淨。
    const fn = vi.fn().mockResolvedValue(undefined)
    const { value: polling } = inScope(() => usePolling(fn, { intervalMs: 1000 }))

    polling.start()
    polling.start()
    await vi.advanceTimersByTimeAsync(1000)

    expect(fn).toHaveBeenCalledTimes(1)
  })
})

describe('停止', () => {
  it('stops scheduling after stop()', async () => {
    const fn = vi.fn().mockResolvedValue(undefined)
    const { value: polling } = inScope(() => usePolling(fn, { intervalMs: 1000 }))

    polling.start()
    await vi.advanceTimersByTimeAsync(1000)
    polling.stop()
    await vi.advanceTimersByTimeAsync(5000)

    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('does not reschedule when a run settles after stop()', async () => {
    // stop() 發生在請求已經出發之後是最常見的情況（使用者按上一頁）。
    // 只清計時器而不記下「已停」，那個還在飛的請求回來時會再排下一次。
    let resolveRun: (() => void) | undefined
    const fn = vi.fn().mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveRun = resolve
        }),
    )
    const { value: polling } = inScope(() => usePolling(fn, { intervalMs: 1000 }))

    polling.start()
    await vi.advanceTimersByTimeAsync(1000)
    polling.stop()
    resolveRun?.()
    await vi.advanceTimersByTimeAsync(5000)

    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('stops when the owning scope is disposed', async () => {
    // 元件卸載即停：忘了在 onUnmounted 裡 stop 的頁面，在 SPA 裡會一路輪詢到
    // 使用者關掉分頁——而且看不出來。
    const fn = vi.fn().mockResolvedValue(undefined)
    const { value: polling, dispose } = inScope(() => usePolling(fn, { intervalMs: 1000 }))

    polling.start()
    dispose()
    await vi.advanceTimersByTimeAsync(5000)

    expect(fn).not.toHaveBeenCalled()
  })
})

describe('失敗處理', () => {
  it('backs off exponentially and recovers after a success', async () => {
    const fn = vi
      .fn()
      .mockRejectedValueOnce(new Error('boom'))
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValue(undefined)
    const { value: polling } = inScope(() => usePolling(fn, { intervalMs: 1000 }))

    polling.start()
    await vi.advanceTimersByTimeAsync(1000)
    expect(fn).toHaveBeenCalledTimes(1)

    // 第一次失敗後間隔加倍：1000 還不夠。
    await vi.advanceTimersByTimeAsync(1000)
    expect(fn).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1000)
    expect(fn).toHaveBeenCalledTimes(2)

    // 第二次失敗後是 4000。
    await vi.advanceTimersByTimeAsync(4000)
    expect(fn).toHaveBeenCalledTimes(3)

    // 這次成功——間隔回到 1000，不留著退讓後的節奏。
    await vi.advanceTimersByTimeAsync(1000)
    expect(fn).toHaveBeenCalledTimes(4)
  })

  it('gives up after too many consecutive failures', async () => {
    const fn = vi.fn().mockRejectedValue(new Error('boom'))
    const onGiveUp = vi.fn()
    const { value: polling } = inScope(() =>
      usePolling(fn, { intervalMs: 1000, maxFailures: 3, onGiveUp }),
    )

    polling.start()
    await vi.advanceTimersByTimeAsync(1000) // 1
    await vi.advanceTimersByTimeAsync(2000) // 2
    await vi.advanceTimersByTimeAsync(4000) // 3 → 放棄

    expect(fn).toHaveBeenCalledTimes(3)
    expect(polling.isPolling.value).toBe(false)
    // 放棄要有人知道：畫面得從「更新中」變成「連線有問題，重新整理試試」。
    expect(onGiveUp).toHaveBeenCalledTimes(1)
    expect(onGiveUp.mock.calls[0]?.[0]).toBeInstanceOf(Error)

    await vi.advanceTimersByTimeAsync(60_000)
    expect(fn).toHaveBeenCalledTimes(3)
  })

  it('caps the backoff so a long outage still retries at a sane rate', async () => {
    const fn = vi.fn().mockRejectedValue(new Error('boom'))
    const { value: polling } = inScope(() =>
      usePolling(fn, { intervalMs: 1000, maxIntervalMs: 4000, maxFailures: 10 }),
    )

    polling.start()
    await vi.advanceTimersByTimeAsync(1000) // 1
    await vi.advanceTimersByTimeAsync(2000) // 2
    await vi.advanceTimersByTimeAsync(4000) // 3
    await vi.advanceTimersByTimeAsync(4000) // 4：已達上限，不再加倍
    expect(fn).toHaveBeenCalledTimes(4)
  })

  it('never lets a rejection escape as an unhandled promise', async () => {
    // 輪詢的 fn 失敗是預期內的事（後端重啟中）。讓它冒出去的話，瀏覽器主控台
    // 會出現 unhandledrejection，而 Sentry 之類的收集器會把它當成程式崩潰。
    const fn = vi.fn().mockRejectedValue(new Error('boom'))
    const onUnhandled = vi.fn()
    process.on('unhandledRejection', onUnhandled)
    const { value: polling } = inScope(() => usePolling(fn, { intervalMs: 1000 }))

    polling.start()
    await vi.advanceTimersByTimeAsync(1000)
    await Promise.resolve()

    process.off('unhandledRejection', onUnhandled)
    expect(onUnhandled).not.toHaveBeenCalled()
  })
})

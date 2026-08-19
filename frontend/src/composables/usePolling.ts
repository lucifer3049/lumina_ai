/**
 * 定期重複一件事，直到叫停（1E-2；03 §2）。
 *
 * ETL 進度沒有推播管道——文件狀態只能問（09 §2.3 的 `GET /documents/{id}` 與列表）。
 * 這個 composable 是「問」的節奏控制，四個性質各自對應一種只在真實環境才發作的故障：
 *
 * 1. **跑完才排下一次**（不是 `setInterval`）：後端慢於間隔時，`setInterval` 會讓請求
 *    疊起來，越疊越慢，最後把使用者的其他操作也拖住。
 * 2. **停得掉**：SPA 裡忘了停的輪詢會一路打到分頁關閉。它還會在 session 過期後不斷
 *    觸發 refresh，把「安靜地閒置」變成「安靜地重新登入」。
 * 3. **失敗指數退讓**：後端重啟中每 3 秒重試一次是在幫倒忙。
 * 4. **失敗夠多次就放棄並通知**：無限重試的頁面永遠不會告訴使用者「壞了」。
 */
import { onScopeDispose, ref, type Ref } from 'vue'

export interface PollingOptions {
  /** 成功時的間隔。 */
  intervalMs: number
  /** 退讓的上限，預設是間隔的 8 倍——長時間斷線時仍以合理頻率探。 */
  maxIntervalMs?: number
  /** 連續失敗幾次就放棄，預設 5。 */
  maxFailures?: number
  /** 放棄時的通知：畫面要從「更新中」換成「連線有問題」。 */
  onGiveUp?: (error: unknown) => void
}

export interface Polling {
  isPolling: Ref<boolean>
  start: () => void
  stop: () => void
}

export function usePolling(fn: () => void | Promise<void>, options: PollingOptions): Polling {
  const { intervalMs, maxFailures = 5, onGiveUp } = options
  const maxIntervalMs = options.maxIntervalMs ?? intervalMs * 8

  const isPolling = ref(false)
  let timer: ReturnType<typeof setTimeout> | null = null
  let failures = 0
  /**
   * 每次 start/stop 都換一個代號。已經出發的那一輪回來時若代號對不上就默默退場
   * ——`stop()` 常常發生在請求已經在路上之後（使用者按上一頁），只清計時器的話，
   * 那個回應會再排下一次，於是「停」了的輪詢還活著。
   */
  let generation = 0

  function schedule(delayMs: number, ownGeneration: number): void {
    timer = setTimeout(() => {
      void run(ownGeneration)
    }, delayMs)
  }

  async function run(ownGeneration: number): Promise<void> {
    try {
      await fn()
    } catch (error) {
      if (ownGeneration !== generation) {
        return
      }
      failures += 1
      if (failures >= maxFailures) {
        stop()
        onGiveUp?.(error)
        return
      }
      // 2 的次方：1 次失敗 → 2 倍，2 次 → 4 倍…… 上限之後不再加倍。
      schedule(Math.min(intervalMs * 2 ** failures, maxIntervalMs), ownGeneration)
      return
    }

    if (ownGeneration !== generation) {
      return
    }
    // 成功即復位：不留著退讓後的節奏，否則一次抖動會讓之後的進度更新都慢半拍。
    failures = 0
    schedule(intervalMs, ownGeneration)
  }

  /** 第一次不立刻跑：呼叫端剛剛才載過資料，馬上再問一次是白打的請求。 */
  function start(): void {
    if (isPolling.value) {
      return
    }
    isPolling.value = true
    failures = 0
    generation += 1
    schedule(intervalMs, generation)
  }

  function stop(): void {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
    generation += 1
    isPolling.value = false
  }

  // 元件卸載即停。第二個參數 `true` = 不在 scope 裡使用時不要警告（測試會在
  // 自建的 effectScope 內建立它，而 store 之類的地方也可能沒有 scope）。
  onScopeDispose(stop, true)

  return { isPolling, start, stop }
}

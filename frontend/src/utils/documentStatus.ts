/**
 * 文件狀態的顯示與判定（1E-2；08 §2 狀態機）。
 *
 * 後端的 `DocumentOut.status` 型別是**字串**——openapi.json 沒有 enum（Django 的
 * `status` 是 TextField），所以前端必然要自己有一份清單。它有一個刻意的預設方向：
 *
 * > **沒見過的狀態一律算「還在跑」。**
 *
 * 反過來（未知即完成）的話，後端之後在中間插一個新階段時，前端會在那個狀態停止
 * 輪詢——畫面永遠停在半路，而且不報錯，因為對前端來說一切正常。現在這個方向最壞
 * 只是多問幾次，而下一次狀態變成 ready 就自己收斂了。方向的選法與 router guard 的
 * 「預設受保護」同一條原則：漏掉時要往安全的那一側倒。
 */

/** 08 §2 的成功路徑，順序即進度。`failed` 不在其中——它是另一種結局，不是一格進度。 */
export const DOCUMENT_STAGES = [
  'uploaded',
  'parsing',
  'cleaned',
  'chunked',
  'embedding',
  'ready',
] as const

export type DocumentStage = (typeof DOCUMENT_STAGES)[number]

/** 終點：只有這兩個。輪詢停不停、動作鈕開不開，都以它為準。 */
const SETTLED_STATUSES: ReadonlySet<string> = new Set(['ready', 'failed'])

/**
 * 後端在這幾個狀態下拒絕 reingest（`services/knowledge/documents.py` 的
 * `_IN_PROGRESS_STATUSES`）：重跑會讓兩個 job 寫同一份文件的 chunk。
 *
 * `chunked` **不在**裡面——那時 chunk 已寫完，重跑是安全的，而且它是 embedding
 * 沒被觸發時唯一的恢復入口。前端照抄這份清單只是為了把按鈕停用（少一次註定
 * 409 的往返）；最終裁決仍在後端，兩邊有時間差是正常的。
 */
const REINGEST_BLOCKED_STATUSES: ReadonlySet<string> = new Set(['parsing', 'cleaned', 'embedding'])

const STATUS_LABELS: Readonly<Record<string, string>> = {
  uploaded: '已收下，排隊中',
  parsing: '解析中',
  cleaned: '切塊中',
  chunked: '已切塊，等待向量',
  embedding: '建立向量中',
  ready: '可以使用',
  failed: '處理失敗',
}

/** 走到終點了嗎（成功或失敗都算）。 */
export function isDocumentSettled(status: string): boolean {
  return SETTLED_STATUSES.has(status)
}

export function isDocumentFailed(status: string): boolean {
  return status === 'failed'
}

/** 可以按「重新處理」嗎。 */
export function canReingest(status: string): boolean {
  return !REINGEST_BLOCKED_STATUSES.has(status)
}

/**
 * 給人看的狀態。未知狀態**保留原字串**——那是使用者唯一能回報的線索，
 * 換成「未知」等於把它丟掉。
 */
export function documentStatusLabel(status: string): string {
  return STATUS_LABELS[status] ?? `處理中（${status}）`
}

/** 進度條用的序位；不在成功路徑上（failed、未知）一律 -1，由呼叫端改顯示別的東西。 */
export function documentStageIndex(status: string): number {
  return DOCUMENT_STAGES.indexOf(status as DocumentStage)
}

/** 進度百分比（0–100）。不在成功路徑上時回 0。 */
export function documentProgressPercent(status: string): number {
  const index = documentStageIndex(status)
  if (index < 0) {
    return 0
  }
  return Math.round((index / (DOCUMENT_STAGES.length - 1)) * 100)
}

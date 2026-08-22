/**
 * 對話清單的搜尋過濾與日期分組（ChatGPT 慣例：今天／昨天／前 7 天／前 30 天／更早）。
 *
 * 分組鍵是 `last_message_at`（清單的排序鍵也是它——同一個鍵，順序與分組才不會互相
 * 打架）。`null`（剛建立、還沒說話）視同現在：一個沒有任何訊息的對話幾乎必然是
 * 剛開的那一個。釘選的自成一組、永遠在最上（釘選的意義就是不被時間沖走）。
 */
import type { ConversationOut } from '@/types/models'

export interface ConversationGroup {
  label: string
  items: ConversationOut[]
}

const DAY_MS = 24 * 60 * 60 * 1000

const GROUP_ORDER = ['已釘選', '今天', '昨天', '前 7 天', '前 30 天', '更早'] as const

function startOfDay(at: Date): number {
  const day = new Date(at)
  day.setHours(0, 0, 0, 0)
  return day.getTime()
}

/** 單筆對話的相對日期標籤（首頁「最近對話」也用它，與側欄分組共用同一套日界）。 */
export function relativeDayLabel(lastMessageAt: string | null, now: Date = new Date()): string {
  return labelFor(lastMessageAt, now)
}

function labelFor(lastMessageAt: string | null, now: Date): string {
  const at = lastMessageAt === null ? now.getTime() : new Date(lastMessageAt).getTime()
  const today = startOfDay(now)
  if (at >= today) {
    return '今天'
  }
  if (at >= today - DAY_MS) {
    return '昨天'
  }
  if (at >= today - 7 * DAY_MS) {
    return '前 7 天'
  }
  if (at >= today - 30 * DAY_MS) {
    return '前 30 天'
  }
  return '更早'
}

/** 標題子字串過濾（前端；訊息內文的全文搜尋需要後端端點，不在這裡假裝）。 */
export function filterConversations(
  items: readonly ConversationOut[],
  query: string,
): ConversationOut[] {
  const needle = query.trim().toLowerCase()
  if (needle === '') {
    return [...items]
  }
  return items.filter((item) => item.title.toLowerCase().includes(needle))
}

/** 組內保持輸入順序（後端已是最近在前）；空組不出現。 */
export function groupConversations(
  items: readonly ConversationOut[],
  now: Date = new Date(),
): ConversationGroup[] {
  const buckets = new Map<string, ConversationOut[]>()
  for (const item of items) {
    const label = item.pinned ? '已釘選' : labelFor(item.last_message_at, now)
    const bucket = buckets.get(label) ?? []
    bucket.push(item)
    buckets.set(label, bucket)
  }
  return GROUP_ORDER.filter((label) => buckets.has(label)).map((label) => ({
    label,
    items: buckets.get(label) ?? [],
  }))
}

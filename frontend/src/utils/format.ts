/**
 * 純顯示用的格式化（03 §2 utils：純函式，不碰狀態）。
 */

const UNITS = ['B', 'KB', 'MB', 'GB'] as const

/**
 * 檔案大小。以 1024 為進位（與作業系統顯示一致），一位小數就夠——
 * 使用者要的是「這份比較大」，不是精確位元組數。
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return '0 B'
  }
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${UNITS[unit]}`
}

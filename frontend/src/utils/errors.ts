/**
 * 錯誤訊息的統一取法（1E-2）。
 *
 * `ApiError.detail` 是後端寫給人看的那一句（09 §1.3），直接顯示即可；5xx 的 detail
 * 已經在後端換成通用敘述，不含內部細節。非 ApiError 代表程式錯誤——那時不能顯示
 * `error.message`（那是給開發者的，常常是 undefined 的屬性名），改用一句固定的話。
 */
import { ApiError } from '@/api/client'

export function errorMessage(error: unknown, fallback = '操作失敗，請稍後重試'): string {
  return error instanceof ApiError ? error.detail : fallback
}

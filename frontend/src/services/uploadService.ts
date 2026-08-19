/**
 * 文件上傳（1E-2；03 §2、09 §3.1）。
 *
 * 這是前端唯一「請求主體不是 JSON」的路徑，三件事集中在這裡，views 只負責顯示：
 *
 * 1. **Content-Type 留給瀏覽器填**：multipart 的 boundary 是 `FormData` 送出時才產生
 *    的。手寫 `'multipart/form-data'` 會讓後端一個欄位都解不出來，症狀是「422 少了
 *    file」而檔案明明有選——這條路徑最常見的錯。
 * 2. **大小上限在送出前擋**：讓使用者等一次註定失敗的 40MB 上傳沒有意義。這**不是**
 *    安全檢查，後端仍是最終裁決者（`services/knowledge/uploads.py`）。
 * 3. **把 code 翻成下一步動作**：413/415/409 分別是「換小一點的檔案」「換格式」
 *    「這份已經在了」——三種完全不同的處置，而後端的 detail 只說發生了什麼。
 *
 * 大檔（>32MB）的 presigned 分塊直傳（09 §3.1）後端尚未實作，本檔刻意不先鋪路。
 */
import { ApiError, request } from '@/api/client'
import type { DocumentOut } from '@/types/models'

/**
 * 單請求上傳上限，與後端 `MAX_UPLOAD_BYTES` 同一個值（09 §3.1 的界線）。
 * 兩邊都要有，但只有後端的那份算數。
 */
export const MAX_UPLOAD_BYTES = 32 * 1024 * 1024

/**
 * 上傳的逾時預算。刻意大於全域的 15s（11 §4.1）——那個值是給「一問一答」的
 * 請求用的，而這裡送的是最多 32MB 的資料：15s 等於要求 2MB/s 以上，家用上行
 * 網路達不到。這不是把長任務硬撐在 HTTP 上（09 §3.3 的 202 模式）：ETL 本身
 * 是非同步的，這段時間純粹在傳位元組。
 */
export const UPLOAD_TIMEOUT_MS = 120_000

/**
 * 檔案選擇器的過濾條件——與後端白名單（`ACCEPTED_MEDIA_TYPES`）同一組格式。
 * 它只是**提示**：後端以內容（magic bytes）判定型別，副檔名不參與。
 */
export const ACCEPTED_UPLOAD_EXTENSIONS = '.pdf,.docx,.xlsx,.txt,.md'

const SIZE_LIMIT_TEXT = `${MAX_UPLOAD_BYTES / 1024 / 1024}MB`

/** 上傳一份文件到指定 KB，回傳剛建立的文件（狀態為 `uploaded`，ETL 隨後開始）。 */
export async function uploadDocument(kbId: string, file: File): Promise<DocumentOut> {
  if (file.size > MAX_UPLOAD_BYTES) {
    throw new ApiError({
      status: 413,
      code: 'UPLOAD_TOO_LARGE',
      title: '檔案過大',
      detail: `檔案超過單次上傳上限 ${SIZE_LIMIT_TEXT}`,
      requestId: null,
    })
  }

  const form = new FormData()
  // 欄位名 'file' 是後端 `Body_documents_upload` 的契約；檔名由 File 自帶。
  form.append('file', file)

  return request<DocumentOut>(`/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/documents`, {
    method: 'POST',
    body: form,
    timeoutMs: UPLOAD_TIMEOUT_MS,
  })
}

/** 把上傳失敗翻成「使用者現在能做什麼」。 */
export function describeUploadError(error: unknown): string {
  if (!(error instanceof ApiError)) {
    // 走到這裡代表不是 HTTP 失敗而是程式錯誤。仍要回一句話——錯誤畫面本身
    // 不能因為拿到意外的東西而空白。
    return '上傳失敗，請稍後重試'
  }

  switch (error.code) {
    case 'UPLOAD_TOO_LARGE':
      return `檔案超過 ${SIZE_LIMIT_TEXT} 上限，請改上傳較小的檔案`
    case 'UNSUPPORTED_MEDIA_TYPE':
      return '不支援這個檔案格式，目前接受 PDF、Word、Excel、純文字與 Markdown'
    case 'RESOURCE_CONFLICT':
      return '這份文件已經在這個知識庫裡了'
    default:
      // 沒有對應動作時，後端的 detail 至少說得出發生什麼事——比「未知錯誤」有用。
      return error.detail
  }
}

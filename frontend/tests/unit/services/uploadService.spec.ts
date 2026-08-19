/**
 * 驗收：services/uploadService.ts（1E-2；03 §2、09 §3.1、10 §上傳）。
 *
 * 上傳是前端唯一「請求主體不是 JSON」的路徑，它有三個一錯就很難查的地方：
 * multipart 的 Content-Type 必須留給瀏覽器填（boundary）、大小上限要在送出**之前**
 * 擋、以及後端的 413/415/409 各自代表完全不同的使用者動作（換檔案／換格式／別再傳）。
 * 這三件事都在這一層，views 只負責把訊息顯示出來。
 *
 * 一律用 msw 攔在網路層（03 §6.1），不 stub fetch。
 */
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'

import { ApiError } from '@/api/client'
import { MAX_UPLOAD_BYTES, describeUploadError, uploadDocument } from '@/services/uploadService'

const BASE_URL = 'http://api.test'
const KB_ID = '3f9f2b1e-0000-4000-8000-0000000000ab'
const server = setupServer()

const DOC = {
  id: '3f9f2b1e-0000-4000-8000-00000000d001',
  kb_id: KB_ID,
  filename: 'handbook.pdf',
  mime_type: 'application/pdf',
  size_bytes: 12,
  status: 'uploaded',
  doc_version: 1,
  error: null,
}

const problem = (status: number, code: string, detail: string) =>
  HttpResponse.json(
    { type: 'about:blank', title: code, status, detail, code, request_id: 'req-1' },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

describe('送出的請求形狀（09 §3.1 小檔單請求）', () => {
  it('posts multipart to the KB endpoint with the file under the "file" field', async () => {
    let seenName: string | null = null
    let seenText: string | null = null
    server.use(
      http.post(`${BASE_URL}/api/v1/knowledge-bases/${KB_ID}/documents`, async ({ request }) => {
        const form = await request.formData()
        const file = form.get('file')
        // 欄位名是後端 `Body_documents_upload` 的契約（openapi.json）——
        // 改名的話後端回 422，而訊息裡只會說少一個欄位。
        seenName = file instanceof File ? file.name : null
        seenText = file instanceof File ? await file.text() : null
        return HttpResponse.json(DOC, { status: 201 })
      }),
    )

    const file = new File(['hello world!'], 'handbook.pdf', { type: 'application/pdf' })
    await expect(uploadDocument(KB_ID, file)).resolves.toMatchObject({ id: DOC.id })

    // 檔名要原樣送達：後端只拿它當顯示用（型別以內容判定），但使用者是靠它
    // 在列表裡認出自己剛傳的東西。
    expect(seenName).toBe('handbook.pdf')
    expect(seenText).toBe('hello world!')
  })

  it('never sets Content-Type by hand', async () => {
    // multipart 的 Content-Type 必須帶 boundary，而 boundary 是 FormData 送出時
    // 才產生的。手寫 'multipart/form-data' 會讓後端解不出任何欄位，症狀是
    // 「422 少了 file」而檔案明明有選——這是這條路徑最常見的錯。
    let contentType: string | null = 'not-checked'
    server.use(
      http.post(`${BASE_URL}/api/v1/knowledge-bases/${KB_ID}/documents`, ({ request }) => {
        contentType = request.headers.get('Content-Type')
        return HttpResponse.json(DOC, { status: 201 })
      }),
    )

    await uploadDocument(KB_ID, new File(['x'], 'a.txt'))

    expect(contentType).toMatch(/^multipart\/form-data; boundary=/)
  })
})

describe('送出前就擋得掉的失敗', () => {
  it('rejects files over the single-request limit without touching the network', async () => {
    // 32MB 的界線與後端 MAX_UPLOAD_BYTES 同一個值（09 §3.1）。前端先擋是為了
    // 不讓使用者等一次註定失敗的上傳——後端仍是最終裁決者，這裡不是安全檢查。
    // 網路零呼叫由 msw 的 onUnhandledRequest: 'error' 保證（沒註冊任何 handler）。
    const oversized = new File([new Uint8Array(MAX_UPLOAD_BYTES + 1)], 'huge.pdf')

    const error = await uploadDocument(KB_ID, oversized).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('UPLOAD_TOO_LARGE')
    expect((error as ApiError).status).toBe(413)
  })

  it('keeps the limit identical to the backend constant', () => {
    expect(MAX_UPLOAD_BYTES).toBe(32 * 1024 * 1024)
  })
})

describe('後端拒絕時的訊息（09 附錄 A 的 code 才是分支依據）', () => {
  it('passes the ApiError through untouched', async () => {
    server.use(
      http.post(`${BASE_URL}/api/v1/knowledge-bases/${KB_ID}/documents`, () =>
        problem(415, 'UNSUPPORTED_MEDIA_TYPE', '不支援的檔案型別'),
      ),
    )

    const error = await uploadDocument(KB_ID, new File(['x'], 'a.exe')).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('UNSUPPORTED_MEDIA_TYPE')
  })

  it('turns each code into an action the user can take', () => {
    // 三個 code 對應三個完全不同的動作：換一個小一點的檔案／換格式／這份已經在了。
    // 直接顯示後端 detail 也能讀，但少了「下一步做什麼」。
    const of = (code: string, detail = '後端原文') =>
      describeUploadError(new ApiError({ status: 400, code, title: 't', detail, requestId: null }))

    expect(of('UPLOAD_TOO_LARGE')).toContain('32')
    expect(of('UNSUPPORTED_MEDIA_TYPE')).toMatch(/PDF/)
    expect(of('RESOURCE_CONFLICT')).toMatch(/已經/)
  })

  it('falls back to the backend detail for codes it does not know', () => {
    // 未知 code 回空字串或「未知錯誤」等於把唯一的線索丟掉——後端的 detail
    // 至少說得出發生什麼事。
    const message = describeUploadError(
      new ApiError({
        status: 500,
        code: null,
        title: 't',
        detail: '伺服器內部錯誤',
        requestId: 'r',
      }),
    )

    expect(message).toContain('伺服器內部錯誤')
  })

  it('describes non-ApiError failures instead of throwing', () => {
    // 呼叫端在 catch 裡拿到的東西不保證是 ApiError（程式錯誤也會落到那裡）。
    // 這個函式若對它們丟例外，錯誤畫面本身就壞了。
    expect(describeUploadError(new Error('boom'))).not.toBe('')
  })
})

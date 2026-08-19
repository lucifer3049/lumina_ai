/**
 * 驗收：stores/knowledge.ts（1E-2；03 §2、09 §2.3）。
 *
 * 這個 store 是 KB 與文件兩份清單的唯一持有者（03 §1：views 不直接 fetch）。
 * 它要對三件事負責，而三件都與畫面正確性直接相關：
 *
 * 1. **本地清單與後端一致**：建立/刪除/重跑之後不重抓整份清單就要自己更新，
 *    否則使用者按完看不到結果，會再按一次。
 * 2. **切換 KB 時不錯置**：慢的回應不得覆蓋後來的選擇（切 KB 是一秒內會做兩次的事）。
 * 3. **輪詢要停得下來**：`hasPendingDocuments` 是「還要不要問」的唯一依據。
 *
 * 錯誤一律往上拋（ApiError），由 view 決定顯示方式——store 不吞錯，也不自己存
 * 一份 errorMessage：那會讓兩個頁面共用同一個過期的錯誤字串。
 */
import { createPinia, setActivePinia } from 'pinia'
import { HttpResponse, http, delay } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'

import { ApiError } from '@/api/client'
import { DOCUMENT_POLL_INTERVAL_MS, useKnowledgeStore } from '@/stores/knowledge'

const BASE_URL = 'http://api.test'
const KB_A = '3f9f2b1e-0000-4000-8000-00000000000a'
const KB_B = '3f9f2b1e-0000-4000-8000-00000000000b'
const server = setupServer()

const kb = (id: string, name: string, documentCount = 0) => ({
  id,
  name,
  description: '',
  status: 'active',
  document_count: documentCount,
})

const doc = (id: string, filename: string, status: string, kbId = KB_A) => ({
  id,
  kb_id: kbId,
  filename,
  mime_type: 'application/pdf',
  size_bytes: 1024,
  status,
  doc_version: 1,
  error: null,
})

const problem = (status: number, code: string) =>
  HttpResponse.json(
    { type: 'about:blank', title: code, status, detail: '失敗', code, request_id: 'req-1' },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
beforeEach(() => setActivePinia(createPinia()))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

describe('知識庫清單', () => {
  it('loads the list and flips the loading flag back', async () => {
    server.use(
      http.get(`${BASE_URL}/api/v1/knowledge-bases`, () =>
        HttpResponse.json({ items: [kb(KB_A, '法規'), kb(KB_B, '新人訓練')] }),
      ),
    )
    const store = useKnowledgeStore()

    const pending = store.fetchKnowledgeBases()
    expect(store.loadingBases).toBe(true)
    await pending

    expect(store.knowledgeBases.map((k) => k.name)).toEqual(['法規', '新人訓練'])
    expect(store.loadingBases).toBe(false)
  })

  it('clears the loading flag even when the request fails', async () => {
    // 少了 finally 的 loading 旗標會讓失敗後的畫面永遠轉圈——而且重試按鈕
    // 通常就藏在那個轉圈的下面。
    server.use(http.get(`${BASE_URL}/api/v1/knowledge-bases`, () => problem(500, 'INTERNAL_ERROR')))
    const store = useKnowledgeStore()

    await expect(store.fetchKnowledgeBases()).rejects.toBeInstanceOf(ApiError)

    expect(store.loadingBases).toBe(false)
  })

  it('puts a created KB where the backend would put it, without refetching', async () => {
    server.use(
      http.post(`${BASE_URL}/api/v1/knowledge-bases`, async ({ request }) => {
        const body = (await request.json()) as { name: string; description: string }
        return HttpResponse.json(kb(KB_B, body.name), { status: 201 })
      }),
    )
    const store = useKnowledgeStore()
    store.knowledgeBases = [kb(KB_A, '法規')]

    const created = await store.createKnowledgeBase({ name: '新人訓練', description: '' })

    expect(created.id).toBe(KB_B)
    // 最前面而不是最後面：後端的清單是 `-created_at`（repositories/knowledge.py），
    // 加在最後的話重新整理之後它會自己跳到第一列——那個跳動看起來像建錯了。
    expect(store.knowledgeBases.map((k) => k.id)).toEqual([KB_B, KB_A])
  })

  it('replaces the renamed KB in place', async () => {
    server.use(
      http.patch(`${BASE_URL}/api/v1/knowledge-bases/${KB_A}`, () =>
        HttpResponse.json(kb(KB_A, '法規（2026）', 3)),
      ),
    )
    const store = useKnowledgeStore()
    store.knowledgeBases = [kb(KB_A, '法規', 3), kb(KB_B, '新人訓練')]

    await store.updateKnowledgeBase(KB_A, { name: '法規（2026）' })

    // 位置不變：更名後跳到清單最後會讓人以為自己改到別的東西。
    expect(store.knowledgeBases.map((k) => k.name)).toEqual(['法規（2026）', '新人訓練'])
  })

  it('drops a deleted KB from the list', async () => {
    server.use(
      http.delete(
        `${BASE_URL}/api/v1/knowledge-bases/${KB_A}`,
        () => new HttpResponse(null, { status: 204 }),
      ),
    )
    const store = useKnowledgeStore()
    store.knowledgeBases = [kb(KB_A, '法規'), kb(KB_B, '新人訓練')]

    await store.deleteKnowledgeBase(KB_A)

    expect(store.knowledgeBases.map((k) => k.id)).toEqual([KB_B])
  })
})

describe('文件清單', () => {
  it('loads the documents of one KB and remembers which one', async () => {
    server.use(
      http.get(`${BASE_URL}/api/v1/knowledge-bases/${KB_A}/documents`, () =>
        HttpResponse.json({ items: [doc('d1', 'a.pdf', 'ready')] }),
      ),
    )
    const store = useKnowledgeStore()

    await store.fetchDocuments(KB_A)

    expect(store.currentKbId).toBe(KB_A)
    expect(store.documents.map((d) => d.filename)).toEqual(['a.pdf'])
  })

  it('ignores a slow response for a KB the user already left', async () => {
    // 切換 KB 是一秒內會做兩次的動作。沒有這條守門的話，A 的慢回應會蓋掉
    // B 的清單——畫面標題是 B、內容是 A，而且完全不報錯。
    server.use(
      http.get(`${BASE_URL}/api/v1/knowledge-bases/${KB_A}/documents`, async () => {
        await delay(50)
        return HttpResponse.json({ items: [doc('d1', 'a.pdf', 'ready', KB_A)] })
      }),
      http.get(`${BASE_URL}/api/v1/knowledge-bases/${KB_B}/documents`, () =>
        HttpResponse.json({ items: [doc('d2', 'b.pdf', 'ready', KB_B)] }),
      ),
    )
    const store = useKnowledgeStore()

    const slow = store.fetchDocuments(KB_A)
    await store.fetchDocuments(KB_B)
    await slow

    expect(store.currentKbId).toBe(KB_B)
    expect(store.documents.map((d) => d.filename)).toEqual(['b.pdf'])
  })

  it('refreshes silently for polling — no spinner every few seconds', async () => {
    // 輪詢若翻動 loading 旗標，整個表格每 3 秒閃一次骨架畫面。
    let call = 0
    server.use(
      http.get(`${BASE_URL}/api/v1/knowledge-bases/${KB_A}/documents`, () => {
        call += 1
        return HttpResponse.json({
          items: [doc('d1', 'a.pdf', call === 1 ? 'parsing' : 'ready')],
        })
      }),
    )
    const store = useKnowledgeStore()
    await store.fetchDocuments(KB_A)

    const pending = store.refreshDocuments()
    expect(store.loadingDocuments).toBe(false)
    await pending

    expect(store.documents[0]?.status).toBe('ready')
  })

  it('does nothing when refreshing with no KB selected', async () => {
    // 輪詢的 callback 可能在導航之後才跑到；此時沒有 currentKbId，
    // 對 `/knowledge-bases/undefined/documents` 發請求會拿到 404，
    // 而畫面上會冒出一個與使用者動作無關的錯誤。
    const store = useKnowledgeStore()

    await expect(store.refreshDocuments()).resolves.toBeUndefined()
  })
})

describe('上傳、刪除、重跑', () => {
  it('adds the uploaded document to the current list', async () => {
    server.use(
      http.post(`${BASE_URL}/api/v1/knowledge-bases/${KB_A}/documents`, () =>
        HttpResponse.json(doc('d9', 'new.pdf', 'uploaded'), { status: 201 }),
      ),
    )
    const store = useKnowledgeStore()
    store.currentKbId = KB_A
    store.documents = [doc('d1', 'a.pdf', 'ready')]

    const uploaded = await store.uploadDocument(KB_A, new File(['x'], 'new.pdf'))

    expect(uploaded.id).toBe('d9')
    // 新的排在最前面：剛傳完的那份是使用者現在唯一在看的東西。
    expect(store.documents.map((d) => d.id)).toEqual(['d9', 'd1'])
  })

  it('does not touch the list when the upload targets another KB', async () => {
    server.use(
      http.post(`${BASE_URL}/api/v1/knowledge-bases/${KB_B}/documents`, () =>
        HttpResponse.json(doc('d9', 'new.pdf', 'uploaded', KB_B), { status: 201 }),
      ),
    )
    const store = useKnowledgeStore()
    store.currentKbId = KB_A
    store.documents = [doc('d1', 'a.pdf', 'ready')]

    await store.uploadDocument(KB_B, new File(['x'], 'new.pdf'))

    expect(store.documents.map((d) => d.id)).toEqual(['d1'])
  })

  it('removes a deleted document', async () => {
    server.use(
      http.delete(`${BASE_URL}/api/v1/documents/d1`, () => new HttpResponse(null, { status: 204 })),
    )
    const store = useKnowledgeStore()
    store.documents = [doc('d1', 'a.pdf', 'ready'), doc('d2', 'b.pdf', 'ready')]

    await store.deleteDocument('d1')

    expect(store.documents.map((d) => d.id)).toEqual(['d2'])
  })

  it('keeps the list untouched when the delete fails', async () => {
    // 樂觀刪除（先移除再打 API）在 403/409 之後會讓畫面少一列，重新整理又出現。
    server.use(
      http.delete(`${BASE_URL}/api/v1/documents/d1`, () => problem(404, 'RESOURCE_NOT_FOUND')),
    )
    const store = useKnowledgeStore()
    store.documents = [doc('d1', 'a.pdf', 'ready')]

    await expect(store.deleteDocument('d1')).rejects.toBeInstanceOf(ApiError)

    expect(store.documents.map((d) => d.id)).toEqual(['d1'])
  })

  it('puts the reingested document back to the start of the pipeline in place', async () => {
    server.use(
      http.post(`${BASE_URL}/api/v1/documents/d2/reingest`, () =>
        HttpResponse.json({ ...doc('d2', 'b.pdf', 'uploaded'), doc_version: 2 }, { status: 202 }),
      ),
    )
    const store = useKnowledgeStore()
    store.documents = [doc('d1', 'a.pdf', 'ready'), doc('d2', 'b.pdf', 'failed')]

    await store.reingestDocument('d2')

    expect(store.documents.map((d) => d.id)).toEqual(['d1', 'd2'])
    expect(store.documents[1]?.status).toBe('uploaded')
    expect(store.documents[1]?.doc_version).toBe(2)
  })

  it('surfaces the 409 when the document is still being processed', async () => {
    // 後端在 parsing/cleaned/embedding 期間拒絕重跑（services/knowledge/documents.py）。
    // 前端會把按鈕停用，但兩邊的判定有時間差——這個錯誤還是要看得見。
    server.use(
      http.post(`${BASE_URL}/api/v1/documents/d1/reingest`, () =>
        problem(409, 'RESOURCE_CONFLICT'),
      ),
    )
    const store = useKnowledgeStore()
    store.documents = [doc('d1', 'a.pdf', 'parsing')]

    const error = await store.reingestDocument('d1').catch((e: unknown) => e)

    expect((error as ApiError).code).toBe('RESOURCE_CONFLICT')
    expect(store.documents[0]?.status).toBe('parsing')
  })
})

describe('輪詢的依據', () => {
  it('reports pending work while any document is mid-pipeline', async () => {
    const store = useKnowledgeStore()

    store.documents = [doc('d1', 'a.pdf', 'ready'), doc('d2', 'b.pdf', 'embedding')]
    expect(store.hasPendingDocuments).toBe(true)

    store.documents = [doc('d1', 'a.pdf', 'ready'), doc('d2', 'b.pdf', 'failed')]
    expect(store.hasPendingDocuments).toBe(false)

    store.documents = []
    expect(store.hasPendingDocuments).toBe(false)
  })
})

describe('可調參數（15 §4.1 統一設定畫面的預備）', () => {
  it('keeps the poll interval as one named export', () => {
    // 輪詢間隔是「之後要搬進統一設定畫面」的參數之一（13 §3 帶進 1E 的缺口③）。
    // 散在 view 裡的 3000 到那時要一個一個找；這裡先把它收成單一來源。
    expect(typeof DOCUMENT_POLL_INTERVAL_MS).toBe('number')
    expect(DOCUMENT_POLL_INTERVAL_MS).toBeGreaterThanOrEqual(1000)
    expect(DOCUMENT_POLL_INTERVAL_MS).toBeLessThanOrEqual(10_000)
  })
})

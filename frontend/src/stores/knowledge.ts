/**
 * 知識庫與文件的狀態（1E-2；03 §2、09 §2.3）。
 *
 * views 不直接 fetch（03 §1），所以 KB 清單與「當前 KB 的文件清單」這兩份資料
 * 只有這裡持有。它對三件事負責：
 *
 * 1. **本地清單與後端一致**：建立／更名／刪除／重跑之後就地更新，不重抓整份清單。
 *    重抓不只是多一次往返——那段空窗期畫面會閃回舊資料，使用者會以為沒生效而再按一次。
 * 2. **切換 KB 時不錯置**：慢的回應不得覆蓋後來的選擇（見 `fetchDocuments` 的代號守門）。
 * 3. **輪詢的依據**：`hasPendingDocuments` 是「還要不要問」的唯一來源。
 *
 * **錯誤一律往上拋**（`ApiError`），由 view 決定顯示方式。store 不吞錯，也不自己
 * 存一份 errorMessage——那會讓兩個頁面共用同一個過期的錯誤字串。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { request } from '@/api/client'
import { uploadDocument as uploadToApi } from '@/services/uploadService'
import type {
  DocumentListOut,
  DocumentOut,
  KnowledgeBaseCreateIn,
  KnowledgeBaseListOut,
  KnowledgeBaseOut,
  KnowledgeBaseUpdateIn,
} from '@/types/models'
import { isDocumentSettled } from '@/utils/documentStatus'

/**
 * ETL 進度的輪詢間隔。
 *
 * 3 秒是「感覺得到在動」與「不要打擾後端」之間的起手值（13 §1.2：文件值是起始點）。
 * 它是**可調參數**，之後要搬進統一設定畫面（15 §4.1、13 §3 帶進 1E 的缺口③）——
 * 先收成單一具名匯出，那時只要改這裡的來源，不必在各個 view 裡找散落的 3000。
 */
export const DOCUMENT_POLL_INTERVAL_MS = 3_000

const JSON_HEADERS = { 'Content-Type': 'application/json' } as const

export const useKnowledgeStore = defineStore('knowledge', () => {
  const knowledgeBases = ref<KnowledgeBaseOut[]>([])
  const documents = ref<DocumentOut[]>([])
  const currentKbId = ref<string | null>(null)
  const loadingBases = ref(false)
  const loadingDocuments = ref(false)

  /**
   * 文件清單的請求代號。切 KB 是一秒內會做兩次的動作，而回應不保證照順序回來
   * ——沒有這個守門的話，A 的慢回應會蓋掉 B 的清單：標題是 B、內容是 A，完全不報錯。
   */
  let documentsRequestId = 0

  const hasPendingDocuments = computed(() =>
    documents.value.some((document) => !isDocumentSettled(document.status)),
  )

  // ── 知識庫 ──────────────────────────────────────────────────────────────

  async function fetchKnowledgeBases(): Promise<void> {
    loadingBases.value = true
    try {
      const page = await request<KnowledgeBaseListOut>('/api/v1/knowledge-bases')
      knowledgeBases.value = page.items
    } finally {
      // finally 而不是成功路徑：少了它，失敗後的畫面會永遠轉圈，
      // 而重試按鈕通常就藏在那個轉圈的下面。
      loadingBases.value = false
    }
  }

  async function createKnowledgeBase(input: KnowledgeBaseCreateIn): Promise<KnowledgeBaseOut> {
    const created = await request<KnowledgeBaseOut>('/api/v1/knowledge-bases', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(input),
    })
    // 排最前面：後端的清單是 `-created_at`（repositories/knowledge.py 的
    // `list_for_tenant`），加在最後的話重新整理之後它會自己跳到第一列。
    knowledgeBases.value = [created, ...knowledgeBases.value]
    return created
  }

  async function updateKnowledgeBase(
    kbId: string,
    patch: KnowledgeBaseUpdateIn,
  ): Promise<KnowledgeBaseOut> {
    const updated = await request<KnowledgeBaseOut>(
      `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}`,
      { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(patch) },
    )
    // 就地替換：更名後跳到清單最後，會讓人以為自己改到了別的東西。
    knowledgeBases.value = knowledgeBases.value.map((kb) => (kb.id === kbId ? updated : kb))
    return updated
  }

  async function deleteKnowledgeBase(kbId: string): Promise<void> {
    await request<null>(`/api/v1/knowledge-bases/${encodeURIComponent(kbId)}`, { method: 'DELETE' })
    knowledgeBases.value = knowledgeBases.value.filter((kb) => kb.id !== kbId)
    if (currentKbId.value === kbId) {
      // 刪掉的正是現在開著的那個：留著文件清單會讓輪詢繼續問一個不存在的 KB。
      currentKbId.value = null
      documents.value = []
    }
  }

  // ── 文件 ────────────────────────────────────────────────────────────────

  /** `silent` 給輪詢用：翻動 loading 旗標的話，表格每 3 秒閃一次骨架畫面。 */
  async function fetchDocuments(kbId: string, options: { silent?: boolean } = {}): Promise<void> {
    const silent = options.silent === true
    documentsRequestId += 1
    const requestId = documentsRequestId
    currentKbId.value = kbId
    if (!silent) {
      loadingDocuments.value = true
    }
    try {
      const page = await request<DocumentListOut>(
        `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/documents`,
      )
      if (requestId !== documentsRequestId) {
        return // 使用者已經換到別的 KB，這份回應過期了
      }
      documents.value = page.items
    } finally {
      if (requestId === documentsRequestId && !silent) {
        loadingDocuments.value = false
      }
    }
  }

  /**
   * 輪詢用的重抓。沒有選定 KB 就什麼都不做——輪詢的 callback 可能在導航之後才跑到，
   * 那時對 `/knowledge-bases/undefined/documents` 發請求會拿到 404，畫面上會冒出
   * 一個與使用者動作無關的錯誤。
   */
  async function refreshDocuments(): Promise<void> {
    const kbId = currentKbId.value
    if (kbId === null) {
      return
    }
    await fetchDocuments(kbId, { silent: true })
  }

  async function uploadDocument(kbId: string, file: File): Promise<DocumentOut> {
    const created = await uploadToApi(kbId, file)
    if (kbId === currentKbId.value) {
      // 排最前面：剛傳完的那份是使用者現在唯一在看的東西。
      documents.value = [created, ...documents.value]
    }
    return created
  }

  async function deleteDocument(documentId: string): Promise<void> {
    // 先打 API 再移除（不做樂觀刪除）：403/409 之後畫面少一列、重新整理又出現，
    // 那比多等一個往返難解釋得多。
    await request<null>(`/api/v1/documents/${encodeURIComponent(documentId)}`, { method: 'DELETE' })
    documents.value = documents.value.filter((document) => document.id !== documentId)
  }

  /** 重新處理：後端回 202 + 已回到 `uploaded`、`doc_version` +1 的文件。 */
  async function reingestDocument(documentId: string): Promise<DocumentOut> {
    const updated = await request<DocumentOut>(
      `/api/v1/documents/${encodeURIComponent(documentId)}/reingest`,
      { method: 'POST' },
    )
    documents.value = documents.value.map((document) =>
      document.id === documentId ? updated : document,
    )
    return updated
  }

  return {
    knowledgeBases,
    documents,
    currentKbId,
    loadingBases,
    loadingDocuments,
    hasPendingDocuments,
    fetchKnowledgeBases,
    createKnowledgeBase,
    updateKnowledgeBase,
    deleteKnowledgeBase,
    fetchDocuments,
    refreshDocuments,
    uploadDocument,
    deleteDocument,
    reingestDocument,
  }
})

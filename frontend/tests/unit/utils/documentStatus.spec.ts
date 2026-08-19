/**
 * 驗收：utils/documentStatus.ts（1E-2；08 §2 狀態機）。
 *
 * 後端的 `DocumentOut.status` 型別是**字串**（openapi.json 沒有 enum），所以前端
 * 必然要自己有一份狀態清單。這一份清單有一個關鍵的預設方向：**沒見過的狀態算
 * 「還在跑」**。反過來（未知即完成）的話，後端之後新增一個中間狀態時，前端會在
 * 那個狀態停止輪詢，畫面永遠停在半路而且不報錯——而現在這個方向最壞只是多問幾次。
 */
import { describe, expect, it } from 'vitest'

import {
  DOCUMENT_STAGES,
  documentStageIndex,
  documentStatusLabel,
  isDocumentFailed,
  isDocumentSettled,
} from '@/utils/documentStatus'

describe('終點判定', () => {
  it('treats only ready and failed as settled', () => {
    expect(isDocumentSettled('ready')).toBe(true)
    expect(isDocumentSettled('failed')).toBe(true)
    for (const status of ['uploaded', 'parsing', 'cleaned', 'chunked', 'embedding']) {
      expect(isDocumentSettled(status), `${status} 不是終點`).toBe(false)
    }
  })

  it('treats an unknown status as still running', () => {
    // 見檔頭：這個方向是刻意的。
    expect(isDocumentSettled('some_future_stage')).toBe(false)
  })

  it('separates failure from success', () => {
    expect(isDocumentFailed('failed')).toBe(true)
    expect(isDocumentFailed('ready')).toBe(false)
  })
})

describe('顯示', () => {
  it('labels every stage in the 08 §2 pipeline', () => {
    // `cleaned` 常被漏掉（它只在後端 `_IN_PROGRESS_STATUSES` 裡出現過），
    // 漏了的話使用者會在那幾秒看到一個英文代碼。
    expect(DOCUMENT_STAGES).toEqual([
      'uploaded',
      'parsing',
      'cleaned',
      'chunked',
      'embedding',
      'ready',
    ])
    for (const status of [...DOCUMENT_STAGES, 'failed']) {
      const label = documentStatusLabel(status)
      expect(label, `${status} 沒有中文標籤`).not.toBe('')
      expect(label, `${status} 的標籤還是英文代碼`).not.toBe(status)
    }
  })

  it('falls back to the raw status instead of showing nothing', () => {
    expect(documentStatusLabel('some_future_stage')).toContain('some_future_stage')
  })

  it('orders the stages so a progress bar can only move forward', () => {
    expect(documentStageIndex('uploaded')).toBe(0)
    expect(documentStageIndex('embedding')).toBeGreaterThan(documentStageIndex('parsing'))
    expect(documentStageIndex('ready')).toBe(DOCUMENT_STAGES.length - 1)
    // 失敗不是進度上的一點——它是另一種結局，進度條該讓位給錯誤訊息。
    expect(documentStageIndex('failed')).toBe(-1)
    expect(documentStageIndex('some_future_stage')).toBe(-1)
  })
})

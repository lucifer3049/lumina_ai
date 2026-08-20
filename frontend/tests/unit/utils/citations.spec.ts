/**
 * 驗收：utils/citations.ts（1E-3；06 §3.3、13 §3 帶進 1E 的缺口①）。
 *
 * 後端**刻意不改回答的文字**（rag/citation.py）：字是逐字串流出去的，收不回來，而
 * 重寫持久化內容會打破「串流看到的 = 存下來的」。代價是回答裡可能留著對不上任何
 * 來源的 `[c:7]`——**清理是渲染層的責任，也就是這裡**。
 *
 * 沒清的話，畫面上會出現裸標記，而那看起來像壞掉；清錯邊（把真的也拿掉）則是把
 * 唯一的來源線索丟了。所以這一支測試同時盯兩個方向。
 *
 * 輸出是**資料**不是 HTML：LLM 的輸出永不經 v-html（CLAUDE.md 鐵則 10），
 * 元件拿 segments 自己渲染。
 */
import { describe, expect, it } from 'vitest'

import { renderAnswer } from '@/utils/citations'

const citation = (marker: string, docName: string) => ({
  marker,
  chunk_id: `chunk-${marker}`,
  doc_id: `doc-${marker}`,
  doc_name: docName,
  doc_version: 1,
  page: 3,
  heading_path: ['第一章'],
  score: 0.87,
  snippet: '片段內容',
})

describe('切段', () => {
  it('splits text and markers into ordered segments', () => {
    const segments = renderAnswer('年假是 14 天[c:1]。', [citation('1', '人事規章.pdf')])

    expect(segments).toEqual([
      { kind: 'text', text: '年假是 14 天' },
      { kind: 'citation', marker: '1', index: 1, docName: '人事規章.pdf' },
      { kind: 'text', text: '。' },
    ])
  })

  it('keeps two adjacent markers as two segments', () => {
    const segments = renderAnswer('見規章[c:1][c:2]', [
      citation('1', 'a.pdf'),
      citation('2', 'b.pdf'),
    ])

    expect(segments.filter((segment) => segment.kind === 'citation')).toHaveLength(2)
  })

  it('numbers citations by their position in items, not by the marker text', () => {
    // 上標顯示的數字是「來源面板的第幾筆」。用 marker 當顯示值的話，模型跳號
    // （只引用了第 2 段與第 5 段）時畫面上會是 2、5，而面板裡是 1、2。
    const segments = renderAnswer('[c:2] 與 [c:5]', [
      citation('2', 'a.pdf'),
      citation('5', 'b.pdf'),
    ])

    expect(segments.filter((s) => s.kind === 'citation').map((s) => s.index)).toEqual([1, 2])
  })

  it('returns a single text segment when there are no markers', () => {
    expect(renderAnswer('純聊天，沒有引用。', [])).toEqual([
      { kind: 'text', text: '純聊天，沒有引用。' },
    ])
  })
})

describe('幻覺引用的清理（缺口①）', () => {
  it('drops a marker that is not in items', () => {
    // 這是 1D-5 定案的分工：後端只從清單剔除、文字一字不改，畫面上的清理在這裡。
    const segments = renderAnswer('年假 14 天[c:7]。', [citation('1', 'a.pdf')])

    expect(segments).toEqual([
      { kind: 'text', text: '年假 14 天' },
      { kind: 'text', text: '。' },
    ])
    expect(segments.some((segment) => segment.kind === 'citation')).toBe(false)
  })

  it('drops a malformed marker too', () => {
    // `[c:abc]` 對得上 marker 的形狀（後端的正則是 `[^\]\s]+`），但對不上任何一筆。
    expect(renderAnswer('內容[c:abc]', [citation('1', 'a.pdf')])).toEqual([
      { kind: 'text', text: '內容' },
    ])
  })

  it('never leaves a bare marker in the visible text', () => {
    const segments = renderAnswer('a[c:1]b[c:9]c', [citation('1', 'a.pdf')])
    const visible = segments
      .filter((segment) => segment.kind === 'text')
      .map((segment) => segment.text)
      .join('')

    expect(visible).not.toMatch(/\[c:/)
  })
})

describe('不誤傷', () => {
  it('leaves brackets that are not citation markers alone', () => {
    // 回答裡出現方括號是常態（程式碼、陣列、註記）。
    const text = '陣列寫成 items[0]，備註見 [附錄 A]。'

    expect(renderAnswer(text, [])).toEqual([{ kind: 'text', text }])
  })

  it('does not touch text that merely looks similar', () => {
    const text = '設定 [c: 1] 這種帶空白的寫法不是標記'

    expect(renderAnswer(text, [citation('1', 'a.pdf')])).toEqual([{ kind: 'text', text }])
  })

  it('handles an empty answer without inventing segments', () => {
    expect(renderAnswer('', [])).toEqual([])
  })
})

describe('串流中途的文字', () => {
  it('does not swallow a half-arrived marker as if it were text', () => {
    // 串流到一半時字串可能停在 `[c:` ——此刻它既不是可用的引用，也不該當成
    // 正文顯示出來（下一個 token 到達就會補完）。留著會讓上標「閃」一下裸標記。
    const segments = renderAnswer('年假 14 天[c:', [citation('1', 'a.pdf')])

    expect(segments.map((segment) => segment.kind === 'text' && segment.text)).toEqual([
      '年假 14 天',
    ])
  })
})

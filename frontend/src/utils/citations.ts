/**
 * 把回答文字切成「文字」與「引用」兩種段落（1E-3；06 §3.3、13 §3 缺口①）。
 *
 * 後端**刻意不改回答的文字**（rag/citation.py）：字是逐字串流出去的，收不回來，而
 * 重寫持久化內容會打破 1D-4a 釘住的「串流看到的 = 存下來的」。代價是文字裡可能留著
 * 對不上任何來源的 `[c:7]`（模型自己編的編號）——**清掉它是渲染層的責任，也就是這裡**。
 *
 * 兩個方向都要防：留著裸標記看起來像壞掉；把真的也拿掉則是丟了唯一的來源線索。
 *
 * 輸出是**資料不是 HTML**：LLM 的輸出永不經 `v-html`（CLAUDE.md 鐵則 10），
 * 元件拿 segments 自己用 template 渲染。
 */

/** SSE `citations` 事件與 `messages.citations` 共用的形狀（09 §3.2）。 */
export interface CitationItem {
  marker?: string
  chunk_id?: string
  doc_id?: string
  doc_name?: string
  doc_version?: number
  page?: number | null
  heading_path?: string[]
  score?: number
  snippet?: string
  [key: string]: unknown
}

export type AnswerSegment =
  | { kind: 'text'; text: string }
  | { kind: 'citation'; marker: string; index: number; docName: string }

/**
 * 與後端同一份形狀（rag/citation.py 的 `_MARKER`）。
 * `[^\]\s]+` 而不是 `.*`：寬鬆的比對會把 `[c: 1]` 這種帶空白的寫法也當成標記。
 */
const MARKER = /\[c:([^\]\s]+)\]/g

/** 串流到一半可能停在這裡；此刻它既不是引用，也不該當成正文顯示（下個 token 會補完）。 */
const PARTIAL_MARKER = '[c:'

export function renderAnswer(text: string, citations: readonly CitationItem[]): AnswerSegment[] {
  if (text === '') {
    return []
  }

  const visible = dropTrailingPartialMarker(text)
  const segments: AnswerSegment[] = []
  let cursor = 0

  MARKER.lastIndex = 0
  for (let match = MARKER.exec(visible); match !== null; match = MARKER.exec(visible)) {
    pushText(segments, visible.slice(cursor, match.index))
    cursor = match.index + match[0].length

    const marker = match[1] ?? ''
    const position = citations.findIndex((citation) => citation.marker === marker)
    if (position === -1) {
      // 對不上任何來源 = 幻覺引用。後端已經把它從清單剔除（06 §3.3），
      // 文字上的痕跡在這裡消失——不留段落，等於整個標記沒出現過。
      continue
    }
    segments.push({
      kind: 'citation',
      marker,
      // 顯示的數字是**來源面板的第幾筆**，不是 marker 的值：模型跳號時
      // （只引用第 2 段與第 5 段）畫面上會是 2、5 而面板裡是 1、2。
      index: position + 1,
      docName:
        typeof citations[position]?.doc_name === 'string' ? citations[position].doc_name : '',
    })
  }

  pushText(segments, visible.slice(cursor))
  return segments
}

/** 空字串不成段：標記在句首或句尾時會產生它，而空段落只是讓元件多渲染一個節點。 */
function pushText(segments: AnswerSegment[], text: string): void {
  if (text !== '') {
    segments.push({ kind: 'text', text })
  }
}

function dropTrailingPartialMarker(text: string): string {
  const start = text.lastIndexOf(PARTIAL_MARKER)
  if (start === -1) {
    return text
  }
  // 後面還有 `]` 就是完整的標記（或根本不是標記，例如 `[c: 1]`），照原樣留著。
  return text.indexOf(']', start) === -1 ? text.slice(0, start) : text
}

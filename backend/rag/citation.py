"""引用組裝與驗證（06 §3.3、09 §3.2、02 §2 的 `rag/citation.py`，1D-5）。

**hallucination 防線的第二條**：模型在句尾輸出 `[c:編號]`，這裡比對「這個編號有沒有
出現在**本次** context 裡」，對不上的直接剔除並記數。

為什麼非驗不可：`[c:...]` 是模型自己打出來的字，與它產出的其他文字沒有任何不同。
它可以把編號打錯、可以引用上一輪的段落、也可以憑空生一個——而三者在前端都會渲染成
一個看起來很可信的來源。**帶著假來源的答案比沒有來源的答案更糟**，因為它取得了
使用者的信任。

**編號是「本輪的第幾段」（1、2、3…），不是 chunk 的 UUID** —— 2026-08-17 決定，
偏離 06 §3.1 的 `[c:chunk_id]`（記於 13 §3.5）。兩個理由各自成立：

- **錢**。一個 UUID 約 20 個 token，而模型每引用一次就抄一遍；輸出 token 又比輸入貴
  數倍。八段 context ＋ 五次引用，光是編號就佔掉約 240 token，短編號只剩三十幾。
- **準**。叫模型一字不差抄 36 個十六進位字元，它會抄錯；抄錯就被當成幻覺剔掉，
  畫面上少一個**本來是真的**來源。抄一位數不會錯。

編號只在該輪有效（比對的就是該輪清單），而落地與回傳的仍是真 `chunk_id`——歷史紀錄
因此沒有歧義。

**這一層不改回答的文字。** 已經逐字串流出去的東西收不回來，而重寫持久化的內容會讓
「使用者看到的」與「資料庫存的」不一致（1D-4a 釘住的不變式）。原始文字留著也是 3B
評測的原料：要統計模型多常唬爛，靠的就是它。畫面上的清理屬**渲染**——前端把不在
`items` 裡的標記略去（1E）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rag.retrievers.vector import RetrievedChunk

__all__ = [
    "SNIPPET_MAX_CHARS",
    "Citation",
    "CitationResult",
    "assemble_citations",
    "extract_markers",
    "marker_for",
]

# 引用標記的形狀。**`[^\]\s]+` 而不是 `.*`**：寬鬆的比對會把 `[c:]` 的空字串當成一個
# 編號，而空字串永遠對不上任何 chunk——於是每一則回答都多一筆「被剔除的假引用」，
# 把真正的幻覺指標淹沒掉。
_MARKER = re.compile(r"\[c:([^\]\s]+)\]")

# 片段長度上限。片段是給人瞄一眼的，不是整段原文——整段放進去的話，每一筆引用都把
# 一個完整的 chunk 塞進 SSE 事件、`messages` 的 jsonb 與前端的 store。
SNIPPET_MAX_CHARS = 240
_ELLIPSIS = "…"


@dataclass(frozen=True, slots=True)
class Citation:
    """一筆引用。`as_dict()` 的欄位名是 **09 §3.2 的契約**（SSE 事件與
    `messages.citations` 共用同一份），不是內部型別的欄位名。"""

    marker: str
    chunk_id: str
    doc_id: str
    doc_name: str
    doc_version: int
    page: int | None
    heading_path: list[str]
    score: float
    snippet: str

    def as_dict(self) -> dict[str, Any]:
        return {
            # 答案文字裡的 `[c:1]` 要對得回這一筆——前端靠它把標記換成可以點的上標。
            "marker": self.marker,
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "doc_name": self.doc_name,
            # 文件重新上傳（re-ingest）之後，這則舊回答仍說得出當時引用的是第幾版。
            "doc_version": self.doc_version,
            "page": self.page,
            # Markdown 與 xlsx 沒有頁碼，章節路徑是它們唯一說得出位置的東西。
            "heading_path": list(self.heading_path),
            "score": self.score,
            # 06 §3.3 的「來源片段」，同時是一張**當時的照片**：文件之後被改或刪掉，
            # 這則回答仍看得出它當初依據了什麼。
            "snippet": self.snippet,
        }


@dataclass(frozen=True, slots=True)
class CitationResult:
    citations: list[Citation]
    # 被剔除的標記。**要回報而不是安靜丟掉**：它是 06 §3.3 的幻覺指標，也是 1D-5
    # 決定用短編號之後「模型抄不抄得對」的唯一量測（13 §3.5 第 1 項）。
    dropped_markers: list[str]


def marker_for(index: int) -> str:
    """第幾段（0 起算）→ 放進 context 的編號。

    **從 1 開始**：模型看到的是「第幾段」，而人類的第幾段從 1 算起；用 0 起算的話，
    模型有一定機率自己「修正」成 1，而那一筆就變成錯的。
    """
    return str(index + 1)


def extract_markers(text: str) -> list[str]:
    """回答文字裡的引用標記，**照出現順序**、含重複。"""
    return _MARKER.findall(text)


def assemble_citations(text: str, chunks: Sequence[RetrievedChunk]) -> CitationResult:
    """回答 + 本輪 context → 驗證過的引用清單。

    **順序照答案裡出現的順序**，不是檢索分數的順序：引用面板與答案裡的標記是對照著
    看的（第一個出現的標記對應面板第一項），照分數排的話兩邊對不起來，而使用者只能
    自己猜哪個對哪個。

    去重讓同一段只列一次——同一段被引用五次就出現五個一樣的項目，而使用者會以為那是
    五份不同的證據。
    """
    by_marker = {marker_for(index): chunk for index, chunk in enumerate(chunks)}

    citations: list[Citation] = []
    dropped: list[str] = []
    used: set[str] = set()
    for marker in extract_markers(text):
        chunk = by_marker.get(marker)
        if chunk is None:
            # 對不上的一律是幻覺——包含模型自己編了一個 UUID 的情況。**不能 raise**：
            # 那會讓一次生成因為模型多打了幾個字而整輪失敗。
            if marker not in dropped:
                dropped.append(marker)
            continue
        if marker in used:
            continue
        used.add(marker)
        citations.append(_citation(marker, chunk))

    return CitationResult(citations=citations, dropped_markers=dropped)


def _citation(marker: str, chunk: RetrievedChunk) -> Citation:
    return Citation(
        marker=marker,
        chunk_id=str(chunk.chunk_id),
        doc_id=str(chunk.document_id),
        doc_name=chunk.document_name,
        doc_version=chunk.doc_version,
        page=chunk.page,
        heading_path=list(chunk.heading_path),
        score=chunk.score,
        snippet=_snippet(chunk.content),
    )


def _snippet(content: str) -> str:
    text = content.strip()
    if len(text) <= SNIPPET_MAX_CHARS:
        return text
    return text[:SNIPPET_MAX_CHARS] + _ELLIPSIS

"""驗收：引用組裝與驗證（06 §3.3、09 §3.2，13 §3 工作包 1D-5）。

**這是 hallucination 防線的第二條**（06 §3.3）：模型在句尾輸出 `[c:編號]`，後處理
比對「這個編號有沒有出現在**本次** context 裡」，對不上的直接剔除並記數。

為什麼非驗不可：`[c:...]` 是模型自己打出來的字，與它產出的其他文字沒有任何不同。
它可以把編號打錯、可以引用上一輪的 chunk、也可以憑空生一個——而三者在前端都會渲染成
一個看起來很可信的來源，點下去才發現指向不存在的東西。**帶著假來源的答案比沒有來源的
答案更糟**，因為它取得了使用者的信任。

**編號是「本輪的第幾段」（1、2、3…），不是 chunk 的 UUID**——2026-08-17 決定，
偏離 06 §3.1 的 `[c:chunk_id]`。兩個理由各自成立：

- **錢**。一個 UUID 約 20 個 token，而模型每引用一次就要抄一遍；輸出 token 又比輸入
  貴好幾倍。八段 context ＋ 五次引用，光是編號就佔掉約 240 個 token，換成 `[c:3]`
  只剩三十幾個。
- **準**。叫模型一字不差抄 36 個十六進位字元，它會抄錯；抄錯就被當成幻覺剔掉，
  畫面上少一個**本來是真的**的來源。抄一位數不會錯。

編號只在**這一輪**有效（比對的就是這一輪的清單），而落地與回傳的仍然是真正的
`chunk_id`——歷史紀錄因此沒有歧義。

四件事錯了都不會有例外：

1. **沒有比對就直接組裝**。假引用一路到前端，而它看起來與真的一模一樣。
2. **比對的是「全部的 chunk」而不是本次 context**。上一輪的 chunk 會被當成有效——
   而那一段模型這次根本沒看到，等於用一個沒讀過的來源背書。
3. **重複的標記組出重複的來源**。同一段被引用五次，引用面板就出現五個一樣的項目。
4. **順序不是模型引用的順序**。引用面板的排列與答案裡的標記對不上，使用者要自己
   猜哪個對哪個。
"""

from __future__ import annotations

import uuid

from rag.citation import SNIPPET_MAX_CHARS, assemble_citations, extract_markers, marker_for
from rag.retrievers.vector import RetrievedChunk


def _chunk(
    *,
    content: str = "員工請假應於三日前提出申請",
    page: int | None = 7,
    score: float = 0.82,
    document_name: str = "人事規章.pdf",
    doc_version: int = 1,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_name=document_name,
        doc_version=doc_version,
        content=content,
        score=score,
        page=page,
        heading_path=["人事規章", "請假"],
    )


class TestMarkerFor:
    def test_markers_start_at_one(self) -> None:
        """從 1 開始而不是 0：模型看到的是「第幾段」，而人類的第幾段從 1 算起。
        用 0 起算的話，模型有一定機率自己「修正」成 1——而那一筆就變成錯的。"""
        assert marker_for(0) == "1"
        assert marker_for(7) == "8"

    def test_a_marker_is_short(self) -> None:
        """短編號是這次的省錢決定（見模組 docstring）。一旦有人把它改回 UUID，
        引用的 token 成本會漲回約七倍，而**沒有任何測試會因此變紅**——除了這一條。"""
        assert len(marker_for(0)) <= 3


class TestExtractMarkers:
    def test_it_finds_markers_in_the_order_they_appear(self) -> None:
        markers = extract_markers("甲說法 [c:1]。乙說法 [c:2][c:3]。")

        assert markers == ["1", "2", "3"]

    def test_a_marker_without_an_id_is_not_a_marker(self) -> None:
        """`[c:]` 與 `[c: x]` 不算。**寬鬆的比對會把空字串當成一個編號**，而空字串
        永遠對不上任何 chunk——於是每一則回答都多一筆「被剔除的假引用」，把真正的
        幻覺指標淹沒掉。"""
        assert extract_markers("這裡沒有 [c:] 也沒有 [c: 空白] 或 [citation]") == []

    def test_plain_text_has_no_markers(self) -> None:
        assert extract_markers("知識庫中找不到相關內容。") == []


class TestAssembleCitations:
    def test_it_builds_the_sse_payload_shape(self) -> None:
        """欄位名是 **09 §3.2 的契約**（`citations` 事件與 `messages.citations` 共用
        同一份），不是內部型別的欄位名。改了等於改 API 契約與前端引用面板。"""
        chunk = _chunk()

        result = assemble_citations("年假 14 天 [c:1]。", [chunk])

        assert [entry.as_dict() for entry in result.citations] == [
            {
                # 答案文字裡的 `[c:1]` 要對得回這一筆——前端靠它把標記換成上標①。
                "marker": "1",
                "chunk_id": str(chunk.chunk_id),
                "doc_id": str(chunk.document_id),
                "doc_name": "人事規章.pdf",
                # 文件版本：文件重新上傳之後，這則舊回答仍說得出當時引用的是第幾版。
                "doc_version": 1,
                "page": 7,
                # 章節路徑（2026-08-17 加）：Markdown 與 xlsx 沒有頁碼，章節是唯一
                # 說得出位置的東西。資料本來就在 chunk 的 meta 裡，只是先前沒送出去。
                "heading_path": ["人事規章", "請假"],
                "score": 0.82,
                # 09 §3.2 的五欄之外多這一個：06 §3.3 要求引用面板呈現「來源片段與
                # 頁碼」，而片段只有這裡拿得到。它同時是一張**當時的照片**——文件
                # 之後被改或刪掉，這則回答仍看得出它當初依據了什麼。
                "snippet": "員工請假應於三日前提出申請",
            }
        ]

    def test_a_marker_that_is_not_in_this_context_is_dropped(self) -> None:
        """**幻覺引用直接剔除**（06 §3.3）。本輪只有一段，模型卻引用了第 9 段。"""
        chunk = _chunk()

        result = assemble_citations("甲 [c:1]。乙 [c:9]。", [chunk])

        assert [entry.chunk_id for entry in result.citations] == [str(chunk.chunk_id)]
        assert result.dropped_markers == ["9"]

    def test_a_marker_that_is_not_a_number_is_dropped(self) -> None:
        """模型也可能自己編一個 UUID（它讀過的其他文件常常那樣寫）。編號的字面
        形狀不是合法編號時同樣是幻覺——**不能讓它變成例外**，那會讓一次生成因為
        模型多打了幾個字而整輪失敗。"""
        result = assemble_citations(f"甲 [c:{uuid.uuid4()}]。", [_chunk()])

        assert result.citations == []
        assert len(result.dropped_markers) == 1

    def test_a_chunk_from_another_turn_is_not_valid(self) -> None:
        """比對的是**本次** context 的清單，不是「這個租戶所有的 chunk」。

        編號按本輪的位置分配，所以上一輪的第 3 段與這一輪的第 3 段是不同的東西——
        而落地的是解析後的真 `chunk_id`，歷史紀錄因此不會有歧義。
        """
        this_turn = _chunk(content="這次給模型看的")

        result = assemble_citations("引用 [c:1]。", [this_turn])

        assert [entry.snippet for entry in result.citations] == ["這次給模型看的"]

    def test_the_same_chunk_cited_twice_yields_one_citation(self) -> None:
        """引用面板一段來源列一次。同一段被引用五次就出現五個一樣的項目，而使用者
        會以為那是五份不同的證據。"""
        result = assemble_citations("甲 [c:1]。乙 [c:1]。", [_chunk()])

        assert len(result.citations) == 1

    def test_citations_follow_the_order_of_the_answer(self) -> None:
        """順序照**答案裡出現的順序**，不是檢索分數的順序。

        引用面板與答案裡的標記是對照著看的：第一個出現的標記對應面板第一項。照分數
        排的話兩邊對不起來，而使用者只能自己猜。
        """
        best = _chunk(content="分數高但後被引用", score=0.9)
        worst = _chunk(content="分數低但先被引用", score=0.4)

        result = assemble_citations("甲 [c:2]。乙 [c:1]。", [best, worst])

        assert [entry.snippet for entry in result.citations] == [
            "分數低但先被引用",
            "分數高但後被引用",
        ]

    def test_a_long_chunk_is_truncated_into_a_snippet(self) -> None:
        """片段是給人瞄一眼的，不是整段原文。

        整段放進去的話，每一筆引用都把一個完整的 chunk 塞進 SSE 事件、`messages`
        的 jsonb 與前端的 store——一則有八個引用的回答會把整個 context 再存一遍。
        """
        chunk = _chunk(content="長" * (SNIPPET_MAX_CHARS + 50))

        result = assemble_citations("甲 [c:1]。", [chunk])

        assert len(result.citations[0].snippet) <= SNIPPET_MAX_CHARS + 1  # 截斷符號

    def test_an_answer_without_markers_has_no_citations(self) -> None:
        """**沒有引用不是錯誤**：context 不足時模型該說「知識庫中找不到相關內容」
        （模板規則 3），而那句話本來就沒有來源可引。"""
        result = assemble_citations("知識庫中找不到相關內容。", [_chunk()])

        assert result.citations == []
        assert result.dropped_markers == []

    def test_no_context_means_no_citations(self) -> None:
        """沒掛 KB 的純聊天路徑（06 §9：不付 RAG 成本）。模型若自己打了標記，
        那百分之百是幻覺。"""
        result = assemble_citations("憑空的 [c:1]。", [])

        assert result.citations == []
        assert result.dropped_markers == ["1"]

"""驗收：查詢組裝、候選合併與 context 選取（06 §3.1／§3.2，13 §3 工作包 1D-5）。

**檢索回來的東西不能整包丟給 LLM。** 1C-4 的 `top_k=40` 是候選數，而 06 §3.2 給
RAG context 的預算是 ~4,500 token——40 個 chunk 每個幾百 token，塞進去只有兩種結果：
provider 以 context window 超限退回（看得見），或前面的指令被擠掉而模型開始自由發揮
（看不見）。**兩者之間要選哪一個都不對，正確做法是在送出之前先裁掉。**

這一層是純函式，住在 `rag/`（鐵則 2：演算法在 rag、SQL 在 repository、編排在 service）。
**它自己沒有任何預設值**——數字全由呼叫端從 `services/rag/params.py` 解析後傳進來
（2026-08-17 的產品決定：可調參數集中在單一來源，見 tests/unit/test_rag_params.py）。

它現在看起來很薄，理由與 `rag/retrievers/vector.py` 相同：Phase 2 的 hybrid 要在
`merge_candidates` 這個**同一個位置**把兩路候選以 RRF 融合，而 rerank 接在它與
`select_context` 之間。形狀先立起來，那時不必動呼叫端。

五件事錯了都不會有例外：

1. **追問查不到東西**。「那病假呢？」單獨拿去檢索，命中的是一組與請假無關的內容——
   而模型會很有禮貌地依據那些內容回答。
2. **多路候選的融合排錯**。順序即相關性，而 `build_context_block` 照原順序輸出——
   最相關的那一段落在 context 中段，正是長 context 最容易被忽略的位置。融合本身
   （RRF）的驗收在 `test_rrf.py`；本檔只驗它前後的兩段：門檻與裁切。
3. **裁切從高分端下手**。剩下的是最不相關的那幾段，而回答看起來只是「答得不好」。
4. **預算算得比實際少**。chunk 的大小是 chunker 用 `estimate_tokens` 量出來的，
   context 預算若用另一套估法，兩邊的數字對不起來——而症狀是偶爾超限。
5. **相對門檻在分數是負的時候仍然生效**。餘弦相似度可以是負的，而「負數的八成」
   比原本還大——門檻一開，所有候選都被砍光。
"""

from __future__ import annotations

import uuid

from etl.tokens import estimate_tokens
from rag.pipeline import build_search_query, gate_by_score, select_context
from rag.retrievers.vector import RetrievedChunk


def _chunk(score: float, *, content: str = "內容", page: int | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        content=content,
        score=score,
        page=page,
        heading_path=[],
    )


class TestBuildSearchQuery:
    """追問的處理——06 §3.1 的 condense 的**免錢版**。

    文件的做法是「用小模型把指代性問句改寫成獨立問句」，那是每一輪多一次 LLM 呼叫。
    1D-5 先做零成本的版本：**檢索時把前一個問題也帶上**。「年假幾天？那病假呢？」
    拿去搜，命中的是請假規定；只拿「那病假呢？」去搜，命中的是一組隨機的內容。

    真正的 condense 排 Phase 2/3C——那時有 golden set，量得出它比這個好多少。
    """

    def test_the_previous_question_joins_the_search(self) -> None:
        query = build_search_query("那病假呢？", previous_questions=["年假幾天？"], history_turns=1)

        assert "年假" in query
        assert "那病假呢？" in query

    def test_history_turns_zero_disables_it(self) -> None:
        """使用者關掉這個行為時，檢索只看當前問題（參數由後台設定，見 test_rag_params）。"""
        query = build_search_query("那病假呢？", previous_questions=["年假幾天？"], history_turns=0)

        assert query == "那病假呢？"

    def test_it_takes_the_most_recent_ones(self) -> None:
        """帶的是**最近的**幾個問題。帶最早的話，長對話裡搜尋看到的永遠是開場白。"""
        query = build_search_query(
            "第三個問題",
            previous_questions=["最早的", "上一個"],
            history_turns=1,
        )

        assert "上一個" in query
        assert "最早的" not in query

    def test_the_first_question_of_a_conversation_stands_alone(self) -> None:
        assert (
            build_search_query("年假幾天？", previous_questions=[], history_turns=1) == "年假幾天？"
        )

    def test_whitespace_only_input_collapses_to_empty(self) -> None:
        """空查詢不能往下走：provider 對空字串各家行為不同，有的回一個沒有意義的
        向量——而那會檢索出一組與問題無關、看起來卻像答案的 chunk。"""
        assert build_search_query("   ", previous_questions=["  "], history_turns=1) == ""


class TestSelectContext:
    def test_it_keeps_the_most_relevant_ones(self) -> None:
        """裁切從**低分端**下手。反過來的話，留下的是最不相關的那幾段——而回答
        看起來只是「答得不好」，沒有任何地方指向裁切。"""
        candidates = [_chunk(1.0 - index / 100, content=f"第{index}段") for index in range(20)]

        selected = select_context(candidates, max_chunks=3, token_budget=10_000)

        assert [chunk.content for chunk in selected] == ["第0段", "第1段", "第2段"]

    def test_it_preserves_relevance_order(self) -> None:
        """順序即相關性——`build_context_block` 照原順序輸出，重排會把最相關的
        那一段推進長 context 的中段。"""
        candidates = [_chunk(0.9, content="甲"), _chunk(0.5, content="乙")]

        selected = select_context(candidates, max_chunks=2, token_budget=10_000)

        assert [chunk.content for chunk in selected] == ["甲", "乙"]

    def test_it_stops_at_the_token_budget(self) -> None:
        """06 §3.2：RAG context 有硬上限。

        **用 `estimate_tokens` 而不是另一套估法**：chunk 的大小是 chunker 拿同一個
        函式量出來的（`etl/chunkers/`），兩邊估法不同時預算的算術就對不起來，而症狀
        是偶爾被 provider 以 context window 超限退回。
        """
        text = "字" * 100  # estimate_tokens：CJK 約 1 token／字
        candidates = [_chunk(1.0 - index / 100, content=text) for index in range(10)]

        selected = select_context(candidates, max_chunks=10, token_budget=250)

        assert len(selected) == 2
        assert sum(estimate_tokens(chunk.content) for chunk in selected) <= 250

    def test_the_top_chunk_survives_a_budget_it_alone_exceeds(self) -> None:
        """**預算比第一段還小時仍然給一段**，不是回空清單。

        回空的話，system prompt 會讓模型誠實回答「知識庫中找不到相關內容」——一個
        明明檢索到了東西卻說沒有的系統，而那是使用者最不可能回報的一種錯誤（它看起來
        像知識庫沒建好）。chunker 的 chunk 上限遠低於 context 預算，所以這條路只在
        預算被調得極低時才會走到；它存在是為了讓那時的行為是可預期的。
        """
        candidates = [_chunk(0.9, content="字" * 500)]

        selected = select_context(candidates, max_chunks=8, token_budget=10)

        assert len(selected) == 1

    def test_nothing_retrieved_gives_an_empty_context(self) -> None:
        assert select_context([], max_chunks=8, token_budget=4500) == []


class TestRelativeScoreGate:
    """相對門檻——Phase 1 唯一安全的「及格線」形式（1D-5 決定）。

    **2B-2 起它套在融合之前、逐路各自套用**（原本在 `select_context` 裡、融合之後）。
    理由是尺度：RRF 之後每一段的分數都是名次倒數和（第 1 名 1/61、第 10 名 1/70），
    彼此的比值全部落在 0.87~1.0 之間——門檻設 0.8 也砍不掉任何東西，設 0.99 則會把
    第三名以後全砍光。兩種都不是使用者要的，而且沒有任何錯誤。套在融合前，比較的
    就還是各路自己的尺度（餘弦相似度、pgroonga 分數），語意與 1D-5 當初定的一致。

    06 §3.1 的絕對門檻 0.3 是 **rerank（cross-encoder）分數**，而 Phase 1 只有餘弦
    相似度。兩者是不同尺度的數字：套上去的結果不是「品質變好」，是每次都回
    「知識庫中找不到相關內容」。

    相對門檻只比較「跟第一名差多少」，因此不吃尺度——換打分方式也不會失效。
    **預設關閉**（`ratio=0`）：它仍然會砍掉東西，開不開由使用者依資料決定。
    2B 接上開源 rerank（`bge-reranker-v2-m3`，MIT）之後，絕對門檻才有意義。
    """

    def test_off_by_default_keeps_everything(self) -> None:
        candidates = [_chunk(0.9, content="甲"), _chunk(0.01, content="乙")]

        assert len(gate_by_score(candidates, min_score_ratio=0.0)) == 2

    def test_it_drops_candidates_far_behind_the_best(self) -> None:
        """「只留下分數接近第一名的那幾張」。第一名 0.9、門檻 0.5 → 低於 0.45 的丟掉。"""
        candidates = [
            _chunk(0.9, content="甲"),
            _chunk(0.5, content="乙"),
            _chunk(0.1, content="丙"),
        ]

        kept = gate_by_score(candidates, min_score_ratio=0.5)

        assert [chunk.content for chunk in kept] == ["甲", "乙"]

    def test_it_never_drops_the_best_candidate(self) -> None:
        """第一名對自己的比值永遠是 1，所以它一定留得下來——**否則門檻調到 1.0 就會
        把全部砍光**，而那時使用者看到的是「這個知識庫突然什麼都答不出來」。"""
        candidates = [_chunk(0.9, content="甲"), _chunk(0.89, content="乙")]

        kept = gate_by_score(candidates, min_score_ratio=1.0)

        assert [chunk.content for chunk in kept] == ["甲"]

    def test_a_negative_best_score_disables_the_gate(self) -> None:
        """**餘弦相似度可以是負的**（兩個向量方向相反）。

        負數的「八成」比原本**大**（-0.2 × 0.8 = -0.16 > -0.2），於是門檻一開就
        把第二名以後全砍掉——而使用者只會看到回答突然變差。比值在負數上沒有意義，
        因此那時直接不套用。
        """
        candidates = [_chunk(-0.2, content="甲"), _chunk(-0.3, content="乙")]

        assert len(gate_by_score(candidates, min_score_ratio=0.8)) == 2

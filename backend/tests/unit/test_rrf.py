"""驗收：RRF 融合（06 §3.1「Hybrid：RRF k=60 → 24」、13 §4 工作包 2B-2）。

**融合是 hybrid 的全部價值所在，也是唯一會悄悄毀掉它的地方。** 兩路檢索各自排好，
但它們的分數**不是同一個尺度**：向量那路是餘弦相似度（0~1 上下），FTS 那路是 pgroonga
的分數（實測可達六位數）。把兩邊的分數直接比大小、加權平均或正規化，結果都由「哪一路
的數字比較大」決定，而不是由相關性決定。

**RRF 只看名次**：每一路的第 r 名貢獻 `1 / (k + r)`，同一段在多路都出現就把貢獻相加。
於是「兩路都覺得不錯」勝過「單路覺得很棒」，而任何一路換打分方式（2B-4 接上 rerank、
或哪天換 embedding 模型）都不會讓融合失效——這正是 06 §3.1 選它而不選加權融合的理由
（「免調權重、對分數尺度不敏感」）。

k=60 是文獻與 06 §3.1 的預設：k 越大，名次之間的差距被壓得越平（第 1 名與第 10 名的
貢獻從 1/61 vs 1/70 只差 13%）；k 越小則越信任各路自己的排序。它是可調參數
（`services/rag/params.py` 的 `rrf_k`），不是寫死的常數。
"""

from __future__ import annotations

import uuid

from rag.pipeline import fuse_candidates
from rag.retrievers.vector import RetrievedChunk


def _chunk(score: float, *, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        content=content,
        score=score,
        page=None,
        heading_path=[],
    )


def _contents(chunks: list[RetrievedChunk]) -> list[str]:
    return [chunk.content for chunk in chunks]


class TestFormula:
    def test_the_score_is_the_sum_of_reciprocal_ranks(self) -> None:
        """第一名 `1/(k+1)`，名次**從 1 起算**。

        從 0 起算會讓第一名變成除以 k、第二名除以 k+1——差距被整體平移，而所有數字
        看起來仍然完全合理。
        """
        one = _chunk(0.9, content="甲")
        two = _chunk(0.1, content="乙")

        fused = fuse_candidates([[one, two]], k=60, limit=10)

        assert fused[0].score == 1 / 61
        assert fused[1].score == 1 / 62

    def test_a_chunk_found_by_both_paths_adds_up(self) -> None:
        """兩路都命中 → 兩份貢獻相加。這是 hybrid 想要的行為的**全部**。"""
        shared = _chunk(0.5, content="共用")
        other = _chunk(0.99, content="單路")

        fused = fuse_candidates([[shared], [other, shared]], k=60, limit=10)

        assert _contents(fused) == ["共用", "單路"]
        assert fused[0].score == 1 / 61 + 1 / 62


class TestScaleInvariance:
    """**RRF 不吃分數尺度**——這是它被選中的理由，也是最該被守住的性質。"""

    def test_multiplying_one_path_by_a_thousand_changes_nothing(self) -> None:
        """FTS 的 pgroonga 分數實測可達六位數，而向量那路是 0~1。任何「先正規化再加權」
        的做法都會在這裡翻車，而翻車的樣子是「答案永遠來自分數比較大的那一路」。"""
        vector = [_chunk(0.9, content="甲"), _chunk(0.8, content="乙")]
        fts = [_chunk(1048579.0, content="丙"), _chunk(3.0, content="丁")]

        fused = fuse_candidates([vector, fts], k=60, limit=10)

        # 兩路的第一名**同分**（各 1/61），誰在前由 tie-break 決定——這裡要驗的是
        # 「前兩名就是兩路各自的第一名」，與那個六位數的分數無關。
        assert set(_contents(fused)[:2]) == {"甲", "丙"}

    def test_only_the_order_within_a_path_matters(self) -> None:
        """同一路的分數換成任何遞減數列，結果都一樣。"""
        first = [_chunk(0.9, content="甲"), _chunk(0.89, content="乙")]
        second = [_chunk(500.0, content="甲"), _chunk(1.0, content="乙")]

        assert _contents(fuse_candidates([first], k=60, limit=10)) == _contents(
            fuse_candidates([second], k=60, limit=10)
        )


class TestBehaviour:
    def test_a_hit_from_both_paths_beats_a_single_path_winner(self) -> None:
        """兩路都排到前面的那一段，勝過只有一路把它排第一的那一段。

        這正是 hybrid 要的：兩種完全不同的方法都覺得「還可以」，比只有一種方法覺得
        「很棒」更可能是答案。1/62 + 1/61 ≈ 0.0325 > 1/61 ≈ 0.0164。
        """
        both = _chunk(0.3, content="兩路都有")
        strong = _chunk(0.99, content="單路第一")

        fused = fuse_candidates([[strong, both], [both]], k=60, limit=10)

        assert _contents(fused) == ["兩路都有", "單路第一"]

    def test_a_single_path_keeps_its_original_order(self) -> None:
        """純向量模式（2B-0 的 baseline、評測的 `--mode vector`）走的是**同一個函式**。

        單路時 RRF 是遞減的名次倒數，因此順序與原本一致——一條路徑服務兩種模式，
        兩份實作才不會漂。
        """
        chunks = [_chunk(0.9, content="甲"), _chunk(0.5, content="乙"), _chunk(0.1, content="丙")]

        assert _contents(fuse_candidates([chunks], k=60, limit=10)) == ["甲", "乙", "丙"]

    def test_k_decides_how_much_a_low_rank_still_counts(self) -> None:
        """k 是「名次差距要壓多平」的旋鈕，而它會**翻轉勝負**。

        單路第一名 vs 兩路第十名：`2/(k+10)` 與 `1/(k+1)` 的大小關係在 k=8 附近交換。
        k 小 = 更信任各路自己的排序；k 大 = 更看重「有多少路都提到它」。06 §3.1 的
        預設 60 站在後者那一邊，而它是可調參數而不是常數，理由就在這條測試裡。
        """
        deep = _chunk(0.1, content="兩路都第十")
        strong = _chunk(0.99, content="單路第一")
        filler_a = [_chunk(0.5, content=f"甲{i}") for i in range(8)]
        filler_b = [_chunk(0.5, content=f"乙{i}") for i in range(9)]
        groups = [[strong, *filler_a, deep], [*filler_b, deep]]

        # 比的是這兩段的**相對名次**，不是整份清單的第一名：另一路的第一名（填充用的
        # 那幾段）與「單路第一」同分，誰在前面由 tie-break 決定，與 k 無關。
        def rank_of(content: str, *, k: int) -> int:
            return _contents(fuse_candidates(groups, k=k, limit=50)).index(content)

        assert rank_of("單路第一", k=1) < rank_of("兩路都第十", k=1)
        assert rank_of("兩路都第十", k=60) < rank_of("單路第一", k=60)

    def test_the_same_chunk_appears_once(self) -> None:
        """重複的代價是**兩份 token 換零份新資訊**——而 hybrid 讓重複變成常態。"""
        shared = _chunk(0.8, content="共用")

        fused = fuse_candidates([[shared], [shared]], k=60, limit=10)

        assert len(fused) == 1

    def test_it_cuts_to_the_limit(self) -> None:
        """06 §3.1 的「→ 24」：融合後只留這麼多進下一關（2B-4 的 rerank 吃的就是它）。

        不裁的話，rerank 要對 80 段做 cross-encoder 推論——那是 11 §4 的延遲預算
        （rerank < 800ms）翻好幾倍。
        """
        groups = [[_chunk(1.0 - i / 100, content=f"第{i}") for i in range(40)]]

        assert len(fuse_candidates(groups, k=60, limit=24)) == 24

    def test_a_tie_goes_to_the_earlier_path(self) -> None:
        """**同分在 hybrid 裡是常態**：兩路的第一名各得 1/61。

        2B-2 實測：以 `chunk_id` 決勝（等於擲骰子）時，24 題的手寫題組有 9 題的正確
        答案被擠下 1~2 名，recall@1 從 0.4375 掉到 0.3333。`groups` 的順序因此就是
        優先序——呼叫端把向量放第一路。
        """
        vector_first = _chunk(0.9, content="向量第一")
        keyword_first = _chunk(1048579.0, content="字面第一")

        fused = fuse_candidates([[vector_first], [keyword_first]], k=60, limit=10)

        assert _contents(fused) == ["向量第一", "字面第一"]

    def test_a_better_rank_wins_a_tie_before_path_order(self) -> None:
        """同分且路數不同時，先比「誰在自己那一路排得更前面」。

        兩路第二名（2/62）與單路第一名 + 單路第三名（1/61 + 1/63）幾乎同分，而前者
        的最好名次是 2、後者是 1——後者贏。這條擋的是「把 group 順序當成唯一決勝依據」
        那種寫法：那會讓第二路永遠吃虧，即使它把某一段排在第一名。
        """
        deep = _chunk(0.5, content="兩路都第二")
        shallow = _chunk(0.5, content="一路第一一路第三")
        filler = _chunk(0.4, content="填充")

        fused = fuse_candidates([[shallow, deep, filler], [filler, deep, shallow]], k=60, limit=10)

        assert _contents(fused)[0] == "一路第一一路第三"

    def test_ties_break_stably(self) -> None:
        """同分時順序必須是決定性的：不然兩次查詢的引用編號會對不上（同
        `merge_candidates` 舊有的第二鍵）。"""
        groups = [[_chunk(0.5, content="甲")], [_chunk(0.5, content="乙")]]

        first = _contents(fuse_candidates(groups, k=60, limit=10))
        second = _contents(fuse_candidates(groups, k=60, limit=10))

        assert first == second

    def test_empty_input_is_not_an_error(self) -> None:
        """一路沒命中是正常情況（FTS 對跨語言問句天然回空），不是失敗。"""
        assert fuse_candidates([], k=60, limit=10) == []
        assert fuse_candidates([[], []], k=60, limit=10) == []

        only_one = [_chunk(0.4, content="甲")]
        assert _contents(fuse_candidates([only_one, []], k=60, limit=10)) == ["甲"]

"""驗收：檢索指標的純函式（13 §4 工作包 2B-0；Phase 2 DoD ②）。

**這幾個函式是 Phase 2 DoD ②「hybrid 檢索評測優於純向量」的裁判**。裁判自己算錯的話，
2B 之後所有「好了多少」的結論都建立在一個沒有人驗過的算式上——而錯誤的方向不會有任何
症狀：分數照樣落在 0~1 之間、照樣隨改動而變動，只是變動的意義不是我們以為的那個。

放 unit 而不是 integration：這裡沒有一行需要 DB。輸入是「檢索回來的名次」與「哪些
是對的」，輸出是數字，中間全是算術——正是 `rag/` 該放的那類東西（鐵則 2）。

四個定義先講清楚，因為它們每一個都有第二種常見寫法，而兩種寫法的數字都「看起來合理」：

1. **recall@k = 前 k 名裡命中的相關段落數 ÷ 相關段落總數**。另一種常見寫法是「有沒有
   命中」（0 或 1），那個在這裡叫 `hit@k`。一題只有一個正解時兩者相同——DRCD 大多如此
   ——所以混用不會被發現，直到手寫題那批出現「一題兩個正解」為止。
2. **MRR 取第一個命中的名次倒數**，名次**從 1 起算**。從 0 起算的話第一名會變成除以
   零，而多數人的修法是「加一」，於是每一題都被悄悄地多算一名。
3. **macro 平均**（每題等權），不是把所有命中加總再除以總數。後者會讓「正解多的題目」
   支配整份分數。
4. **重複的檢索結果不加分**。hybrid 之後同一段可能同時被向量與 FTS 命中，而融合前的
   清單裡它會出現兩次——按次數計分的話，2B-1 接上 FTS 那天分數會自己漲上去，而檢索
   品質一點都沒變。那是最糟的一種假訊號：它出現在我們正要用它證明「hybrid 比較好」
   的那一刻。
"""

from __future__ import annotations

import pytest

from rag.metrics import QuestionOutcome, aggregate, hit_at_k, recall_at_k, reciprocal_rank


def _outcome(retrieved: tuple[str, ...], relevant: tuple[str, ...]) -> QuestionOutcome:
    return QuestionOutcome(question_id="q1", retrieved=retrieved, relevant=frozenset(relevant))


class TestRecall:
    def test_it_is_the_share_of_relevant_passages_found(self) -> None:
        """兩個正解、前 5 名裡找到一個 → 0.5（不是 1.0）。"""
        outcome = _outcome(("p1", "p9", "p8", "p7", "p6"), ("p1", "p2"))

        assert recall_at_k(outcome, 5) == pytest.approx(0.5)

    def test_it_only_looks_at_the_first_k(self) -> None:
        outcome = _outcome(("p9", "p8", "p1"), ("p1",))

        assert recall_at_k(outcome, 2) == 0.0
        assert recall_at_k(outcome, 3) == 1.0

    def test_duplicated_hits_do_not_inflate_it(self) -> None:
        """同一段被兩路各命中一次，仍然只算一次（見模組 docstring 第 4 點）。"""
        outcome = _outcome(("p1", "p1", "p1"), ("p1", "p2"))

        assert recall_at_k(outcome, 3) == pytest.approx(0.5)

    def test_a_short_result_list_is_not_padded(self) -> None:
        """檢索只回 2 筆而 k=10 時，答案是「這 2 筆裡有沒有」，不是除以 10。"""
        outcome = _outcome(("p1", "p2"), ("p1",))

        assert recall_at_k(outcome, 10) == 1.0

    def test_k_must_be_positive(self) -> None:
        outcome = _outcome(("p1",), ("p1",))

        for k in (0, -1):
            with pytest.raises(ValueError):
                recall_at_k(outcome, k)


class TestHitRate:
    def test_it_is_binary(self) -> None:
        """命中幾個都是 1.0——它回答的是「這題有沒有救回來」。"""
        assert hit_at_k(_outcome(("p1", "p2"), ("p1", "p2")), 2) == 1.0
        assert hit_at_k(_outcome(("p1", "p9"), ("p1", "p2")), 2) == 1.0
        assert hit_at_k(_outcome(("p8", "p9"), ("p1",)), 2) == 0.0


class TestReciprocalRank:
    def test_rank_counts_from_one(self) -> None:
        """第一名 → 1.0，第二名 → 0.5（見模組 docstring 第 2 點）。"""
        assert reciprocal_rank(_outcome(("p1", "p2"), ("p1",)), 10) == pytest.approx(1.0)
        assert reciprocal_rank(_outcome(("p2", "p1"), ("p1",)), 10) == pytest.approx(0.5)

    def test_it_takes_the_first_hit_only(self) -> None:
        outcome = _outcome(("p9", "p1", "p2"), ("p1", "p2"))

        assert reciprocal_rank(outcome, 10) == pytest.approx(0.5)

    def test_it_is_zero_when_nothing_relevant_is_retrieved(self) -> None:
        """**0 而不是 None**：MRR 的定義就是「沒找到算 0」，把這種題目排除掉的話，
        檢索越爛的系統分數越高（找不到的題目全部不計入）。"""
        assert reciprocal_rank(_outcome(("p8", "p9"), ("p1",)), 10) == 0.0

    def test_hits_beyond_the_cutoff_do_not_count(self) -> None:
        outcome = _outcome(("p9", "p8", "p1"), ("p1",))

        assert reciprocal_rank(outcome, 2) == 0.0
        assert reciprocal_rank(outcome, 3) == pytest.approx(1 / 3)


class TestOutcome:
    def test_a_question_without_relevant_passages_is_rejected(self) -> None:
        """相關集為空的題目**不能存在**：它的 recall 分母是 0。

        容忍它的話，那一題會被算成 0 分（或被靜默跳過），而那看起來像「檢索找不到」
        ——真相是題組本身壞了。題組的驗證在 `rag/goldenset.py`，這裡是第二道。
        """
        with pytest.raises(ValueError):
            QuestionOutcome(question_id="q1", retrieved=("p1",), relevant=frozenset())


class TestAggregate:
    def test_it_averages_per_question(self) -> None:
        """macro 平均：一題滿分、一題零分 → 0.5，與各題有幾個正解無關。"""
        outcomes = [
            QuestionOutcome("q1", ("p1", "p2"), frozenset({"p1", "p2"})),
            QuestionOutcome("q2", ("p8", "p9"), frozenset({"p3"})),
        ]

        metrics = aggregate(outcomes, ks=(2,))

        assert metrics.question_count == 2
        assert metrics.recall_at[2] == pytest.approx(0.5)
        assert metrics.hit_at[2] == pytest.approx(0.5)
        assert metrics.mrr == pytest.approx(0.5)

    def test_an_empty_outcome_list_is_rejected(self) -> None:
        """**不回 0.0**：0 分與「一題都沒跑」在報告裡長得一模一樣，而後者的原因通常是
        題組路徑打錯——那時我們會以為檢索壞了，去查一個沒有壞的東西。"""
        with pytest.raises(ValueError):
            aggregate([], ks=(5,))

    def test_metric_names_are_flat_and_stable(self) -> None:
        """報告是 JSON，鍵名就是契約——`compare_reports` 靠它比對兩次評測。"""
        outcomes = [QuestionOutcome("q1", ("p1",), frozenset({"p1"}))]

        rendered = aggregate(outcomes, ks=(1, 5)).as_dict()

        assert set(rendered) == {
            "recall@1",
            "recall@5",
            "hit@1",
            "hit@5",
            "mrr",
            "question_count",
        }
        assert rendered["question_count"] == 1

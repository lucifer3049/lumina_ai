"""檢索指標（06 §3、12 §5 的評測、13 §4 工作包 2B-0）。

**這是 Phase 2 DoD ②「hybrid 檢索評測優於純向量」的裁判。** 它算錯的話沒有任何症狀：
分數照樣落在 0~1 之間、照樣隨改動而變動，只是變動的意義不是我們以為的那個。

放 `rag/` 的理由與 `pipeline.py` 相同（鐵則 2）：這裡是「換了資料來源也不會變」的純
算術，不碰 ORM、不認識上層。評測的**編排**（灌語料、跑檢索、寫報告）在
`scripts/eval_retrieval.py`；3B 要把編排搬進 `services/evaluation/` 時，指標仍是同一份
——兩份指標實作遲早會漂，而漂掉的症狀是「同一次檢索，兩支程式給出不同的分數」。

四個定義各有第二種常見寫法，而兩種寫法的數字都「看起來合理」：

1. **recall@k = 前 k 名裡命中的相關段落數 ÷ 相關段落總數**。另一種寫法是「有沒有命中」
   （0 或 1），在這裡叫 `hit_at_k`。一題只有一個正解時兩者相同，所以混用不會被發現，
   直到題組裡出現「一題兩個正解」為止。
2. **MRR 取第一個命中的名次倒數，名次從 1 起算**。從 0 起算會讓第一名變成除以零，而
   多數人的修法是「加一」——於是每一題都被悄悄多算一名。
3. **macro 平均**（每題等權），不是把所有命中加總再除以總數；後者會讓正解多的題目支配
   整份分數。
4. **重複的檢索結果不加分**。hybrid 之後同一段可能同時被向量與 FTS 命中，融合前的清單
   裡它會出現兩次——按次數計分的話，2B-1 接上 FTS 那天分數會自己漲上去而品質沒變。
   那是最糟的一種假訊號：它恰好出現在我們要用它證明「hybrid 比較好」的那一刻。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "QuestionOutcome",
    "RetrievalMetrics",
    "aggregate",
    "hit_at_k",
    "recall_at_k",
    "reciprocal_rank",
]


@dataclass(frozen=True, slots=True)
class QuestionOutcome:
    """一題的檢索結果：回了哪些段落（依名次），以及哪些才是對的。

    ``retrieved`` 是 **passage_id 的序列**而不是 chunk：評測比對的單位是題組定義的段落，
    而 chunk 是我們這邊的實作細節。兩者在 2B-0 是一對一（語料一段 = 一個 chunk），但
    寫成 chunk 的話，日後切塊策略一改，指標就會跟著失去意義。
    """

    question_id: str
    retrieved: tuple[str, ...]
    relevant: frozenset[str]

    def __post_init__(self) -> None:
        if not self.relevant:
            # 相關集為空的題目的 recall 分母是 0。容忍它的話那一題會被算成 0 分（或被
            # 靜默跳過），而那看起來像「檢索找不到」——真相是題組壞了。第一道守門在
            # `rag/goldenset.py`，這裡是第二道，擋的是「程式自己組錯」。
            raise ValueError(f"題目 {self.question_id} 沒有任何正解段落，無法計分")

    def top(self, k: int) -> list[str]:
        """前 k 名，**去重後仍保留名次順序**（見模組 docstring 第 4 點）。"""
        _require_positive(k)
        seen: set[str] = set()
        ordered: list[str] = []
        for passage_id in self.retrieved[:k]:
            if passage_id in seen:
                continue
            seen.add(passage_id)
            ordered.append(passage_id)
        return ordered


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    """一份題組的彙總分數。

    `recall_at` / `hit_at` 以 k 為鍵而不是攤平成字串：程式裡要比大小的是數字，攤平留給
    `as_dict()`——報告是 JSON，那裡的鍵名就是契約（`compare_reports` 靠它比對兩次評測）。
    """

    question_count: int
    recall_at: Mapping[int, float] = field(default_factory=dict)
    hit_at: Mapping[int, float] = field(default_factory=dict)
    mrr: float = 0.0

    def as_dict(self) -> dict[str, float]:
        flat: dict[str, float] = {"question_count": float(self.question_count), "mrr": self.mrr}
        for k, value in self.recall_at.items():
            flat[f"recall@{k}"] = value
        for k, value in self.hit_at.items():
            flat[f"hit@{k}"] = value
        return flat


def recall_at_k(outcome: QuestionOutcome, k: int) -> float:
    """前 k 名裡命中的相關段落數 ÷ 相關段落總數。"""
    found = outcome.relevant.intersection(outcome.top(k))
    return len(found) / len(outcome.relevant)


def hit_at_k(outcome: QuestionOutcome, k: int) -> float:
    """前 k 名裡有沒有任何一個正解（1.0 / 0.0）。

    與 recall 分開列的理由見模組 docstring 第 1 點：它回答的是「這題有沒有被救回來」，
    而 recall 回答的是「救回了幾成」。報表上兩個都要——只看 hit 會看不出多正解題目的
    退步，只看 recall 會看不出「有一半的題目根本沒救回來」。
    """
    return 1.0 if outcome.relevant.intersection(outcome.top(k)) else 0.0


def reciprocal_rank(outcome: QuestionOutcome, k: int) -> float:
    """第一個命中的名次倒數（名次從 1 起算）；前 k 名內沒命中則 0.0。

    **0 而不是「跳過這題」**：把沒找到的題目排除掉的話，檢索越爛的系統分數越高。
    """
    for index, passage_id in enumerate(outcome.top(k), start=1):
        if passage_id in outcome.relevant:
            return 1.0 / index
    return 0.0


def aggregate(outcomes: Sequence[QuestionOutcome], *, ks: Sequence[int]) -> RetrievalMetrics:
    """逐題結果 → 一份彙總分數（macro 平均）。

    **空清單直接拒絕**，不回 0.0：0 分與「一題都沒跑」在報告裡長得一模一樣，而後者的
    原因通常是題組路徑打錯——那時我們會以為檢索壞了，去查一個沒有壞的東西。

    MRR 不吃 `ks`：檢索清單本身就是 top_k，所以「整份清單」即是「@top_k」。額外挑一個
    截斷點的話，報告上的 `mrr` 會與 `recall@k` 用不同的視窗，而兩個數字並排在一起時
    沒有人會想到它們的範圍不同。
    """
    if not outcomes:
        raise ValueError("題組是空的——沒有任何題目可以計分")
    if not ks:
        raise ValueError("至少要指定一個 k")

    count = len(outcomes)
    return RetrievalMetrics(
        question_count=count,
        recall_at={k: sum(recall_at_k(o, k) for o in outcomes) / count for k in ks},
        hit_at={k: sum(hit_at_k(o, k) for o in outcomes) / count for k in ks},
        # `max(..., 1)`：檢索一筆都沒回來的題目仍要計入分母（它是 0 分，不是不存在），
        # 而 `reciprocal_rank` 的 k 必須是正數。
        mrr=sum(reciprocal_rank(o, max(len(o.retrieved), 1)) for o in outcomes) / count,
    )


def _require_positive(k: int) -> None:
    """k ≤ 0 一律當錯誤。

    負數在 Python 的切片裡是合法的（`retrieved[:-1]` 是「去掉最後一名」），於是
    `recall_at_k(outcome, -1)` 會安靜地算出一個看起來正常的分數。
    """
    if k <= 0:
        raise ValueError(f"k 必須是正整數，收到 {k}")

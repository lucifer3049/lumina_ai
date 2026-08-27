"""驗收：評測報告記下 rerank 分數分布（2B-4 結案缺口①，工作包 2B-5）。

2B-4 結案表的第一個缺口：

> **絕對門檻 0.3 仍預設關閉**：條件已具備（分數回到 0~1 尺度），但報告不記 rerank
> 分數，因此沒有分布可裁決；驗證腳本上看到的分離度很大（0.9940 vs 0.0000），而那是
> 4 段的玩具樣本，不足以定門檻。

06 §7 說「除錯與評測**共用同一 trace**」，所以這件事不該是評測腳本自己再算一次：
`rag_trace`（本工作包的另一半）已經逐段記下 cross-encoder 給的分數，評測要做的只是
把它逐題寫進報告。

**分成「正解」與「非正解」兩組是這一份的重點。** 一個混在一起的分布看起來永遠很漂亮
（大部分候選本來就不相關，分數自然低），而門檻要回答的問題是**另一個**：

    有沒有一個數字，砍得掉錯的、又留得住對的？

只看整體分布的話，0.3 會被「大多數候選都低於 0.3」這個事實背書——而它同時砍掉了
一成正解，那一成的症狀是「這個知識庫對某些問題突然說不知道」，沒有任何地方看得出來。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from rag.goldenset import load_corpus, load_goldenset
from rag.metrics import QuestionOutcome

REPO_ROOT = Path(__file__).resolve().parents[3]
EVAL_SCRIPT = REPO_ROOT / "backend" / "scripts" / "eval_retrieval.py"
REPORTS = REPO_ROOT / "backend" / "evaluation" / "reports"


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    """載入評測腳本（理由見 `test_eval_runner.py` 的同名 fixture）。"""
    spec = importlib.util.spec_from_file_location("_eval_retrieval_scores", EVAL_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tiny_dataset(tmp_path: Path) -> tuple[Any, Any]:
    corpus_path = tmp_path / "corpus.jsonl"
    corpus_path.write_text(
        '{"passage_id": "p1", "title": "t1", "text": "第一段"}\n'
        '{"passage_id": "p2", "title": "t2", "text": "第二段"}\n',
        encoding="utf-8",
    )
    questions_path = tmp_path / "questions.jsonl"
    questions_path.write_text(
        '{"question_id": "q1", "question": "問句", "passage_ids": ["p1"], '
        '"language": "zh-Hant", "source": "handwritten"}\n',
        encoding="utf-8",
    )
    return load_goldenset(questions_path), load_corpus(corpus_path)


def _rerank_report(
    runner: ModuleType, tiny_dataset: tuple[Any, Any], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    goldenset, corpus = tiny_dataset
    report = runner.build_report(
        mode="vector+rerank",
        dataset_name="tiny",
        goldenset=goldenset,
        corpus=corpus,
        outcomes=[QuestionOutcome("q1", ("p1", "p2"), frozenset({"p1"}))],
        retrieval={
            "embedding_provider": "gemini",
            "embedding_model": "text-embedding-004",
            "top_k": 20,
            "rerank_provider": "tei",
            "rerank_model": "BAAI/bge-reranker-v2-m3",
        },
        per_question=rows,
    )
    assert isinstance(report, dict)
    return report


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "question_id": "q1",
        "hit_rank": 1,
        "retrieved": ["p1", "p2"],
        "retrieved_chunk_ids": ["c1", "c2"],
        "relevant": ["p1"],
        # 逐段的 cross-encoder 分數，順序與 `retrieved` 對齊。
        "scores": [0.94, 0.02],
    }
    row.update(overrides)
    return row


class TestPerQuestionScores:
    def test_each_row_carries_the_scores(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """逐題留底才追得回去。只留彙總的話，「哪一題的正解剛好卡在門檻邊上」永遠
        查不到——而那一題正是調門檻會弄壞的那一題。"""
        report = _rerank_report(runner, tiny_dataset, [_row()])

        assert report["per_question"][0]["scores"] == [0.94, 0.02]

    def test_the_scores_line_up_with_the_retrieved_passages(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """兩份清單錯位的話，「正解拿了幾分」會取到隔壁那一段的分數——而報告看起來
        完全正常，分布也很漂亮。"""
        with pytest.raises(runner.EvaluationError, match="分數"):
            _rerank_report(runner, tiny_dataset, [_row(scores=[0.94])])

    def test_a_rerank_report_without_scores_is_refused(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """**漏記不是小事**（同 `build_report` 對 `rerank_model` 的處置）：一份沒有
        分數的 rerank 報告照樣比得動、照樣有 recall——半年後要裁決門檻時才發現這一份
        沒得用，而那時 GPU 上跑的模型已經換過了。"""
        with pytest.raises(runner.EvaluationError):
            _rerank_report(runner, tiny_dataset, [_row(scores=None)])


class TestDistribution:
    def test_hits_and_misses_are_summarised_separately(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """**這是能不能裁決門檻的關鍵。**

        門檻要回答的是「有沒有一個數字砍得掉錯的、又留得住對的」。混在一起的分布只
        說得出「大部分候選分數很低」——那句話對任何一個門檻都成立。
        """
        report = _rerank_report(runner, tiny_dataset, [_row()])

        summary = report["rerank_scores"]
        assert summary["hit"]["count"] == 1
        assert summary["miss"]["count"] == 1
        assert summary["hit"]["min"] == pytest.approx(0.94)
        assert summary["miss"]["max"] == pytest.approx(0.02)

    def test_it_reports_the_percentiles_needed_to_pick_a_threshold(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """要裁決的是兩個數字：**正解的低分端**（門檻高於它就會誤砍）與**非正解的
        高分端**（門檻低於它就等於沒砍）。平均數在這裡毫無用處——它被中間那一大坨
        拉著走，而門檻只發生在兩端。
        """
        rows = [
            _row(question_id=f"q{index}", scores=[0.9 - index / 100, 0.1 + index / 100])
            for index in range(20)
        ]

        summary = _rerank_report(runner, tiny_dataset, rows)["rerank_scores"]

        assert set(summary["hit"]) >= {"count", "min", "p05", "p25", "p50", "p75", "p95", "max"}
        assert summary["hit"]["p05"] <= summary["hit"]["p50"] <= summary["hit"]["p95"]

    def test_a_question_without_a_hit_only_feeds_the_miss_side(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """沒命中的題目沒有「正解的分數」可記——把它算進 hit 那一組（例如記成 0）
        會把正解的低分端整個拉下來，於是任何門檻看起來都很安全。"""
        report = _rerank_report(
            runner, tiny_dataset, [_row(retrieved=["p2"], scores=[0.3], hit_rank=None)]
        )

        summary = report["rerank_scores"]
        assert summary["hit"]["count"] == 0
        assert summary["miss"]["count"] == 1

    def test_a_non_rerank_report_has_no_distribution(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """純向量報告掛一個空的 `rerank_scores` 讀起來像「跑了但沒記」（同
        `build_report` 對 `rerank_model` 的既有原則）。"""
        goldenset, corpus = tiny_dataset

        report = runner.build_report(
            mode="vector",
            dataset_name="tiny",
            goldenset=goldenset,
            corpus=corpus,
            outcomes=[QuestionOutcome("q1", ("p1",), frozenset({"p1"}))],
            retrieval={
                "embedding_provider": "gemini",
                "embedding_model": "text-embedding-004",
                "top_k": 20,
            },
        )

        assert "rerank_scores" not in report


class TestCompatibility:
    def test_the_schema_version_moved(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """報告是提交進 repo 的契約，形狀變了就要說。

        **只是新增欄位**，所以舊報告仍然讀得動——但讀報告的人要看得出「這一份為什麼
        沒有分數」是因為它是舊的，而不是因為那次跑掉了。
        """
        assert _rerank_report(runner, tiny_dataset, [_row()])["schema_version"] == 2

    def test_the_2b0_baselines_are_still_comparable(self, runner: ModuleType) -> None:
        """2B-0 的 baseline 是 schema_version 1，而它是**所有比較的基準**——不能因為
        報告加了欄位就變成不可比。可比性看的是題組與模型（`_require_comparable`），
        不是報告的版本。
        """
        baseline = json.loads((REPORTS / "baseline_vector_drcd.json").read_text(encoding="utf-8"))
        candidate = {**baseline, "schema_version": 2}

        comparison = runner.compare_reports(baseline, candidate)

        assert comparison is not None

    def test_the_report_still_serialises_to_json(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        report = _rerank_report(runner, tiny_dataset, [_row()])

        assert json.loads(json.dumps(report, ensure_ascii=False))["rerank_scores"]

"""驗收：離線評測腳本的純邏輯與界線（13 §4 工作包 2B-0）。

腳本本身在 `backend/scripts/eval_retrieval.py`，地位與 `verify_provider.py` 相同：
**手動執行、會打真 API、不進 CI**。理由也相同——評測要有意義就必須用真的 embedding
模型（MockProvider 的向量由雜湊決定，「請假」不會靠近「休假」），而真的模型要花錢、
會因為別人的服務中斷而失敗。接進 CI 的話，紅燈與這次改動無關，久了就沒有人看紅燈了。

本檔驗三件事，全部不需要 DB：

1. **報告的形狀**。報告是 JSON 檔、會被提交進 repo、會被下一次評測拿來比對——它就是
   契約。鍵名漂掉時 `compare_reports` 會安靜地少比幾個指標。
2. **兩份報告可不可比**。題組改了、語料改了、embedding 模型換了，分數就沒有可比性；
   而兩個數字仍然相減得出來。DoD ②「hybrid 優於純向量」若是拿兩把不同的尺量出來的，
   結論不管是什麼都是假的。
3. **baseline 真的落檔且沒有過期**。2B-0 的存在理由就是「在改任何檢索程式之前先把純
   向量的分數記下來」，事後補的基準線沒有對照價值。

**2B-0 只實作 `vector` 模式**：`hybrid`（2B-2）與 `hybrid+rerank`（2B-4）先在模式清單裡
就位但拒絕執行——寫成明確的「尚未實作」比 KeyError 好，也比偷偷跑成純向量好得多，後者
會產生一份標著 hybrid 而其實是向量的報告。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from rag.goldenset import load_corpus, load_goldenset
from rag.metrics import QuestionOutcome

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
EVAL_SCRIPT = BACKEND_ROOT / "scripts" / "eval_retrieval.py"
REPORTS = BACKEND_ROOT / "evaluation" / "reports"
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

# 每個 dataset 一份 baseline：語料不同就是不同的一把尺，混在同一份報告裡的平均分數
# 不代表任何東西。
_BASELINES = {
    "drcd": (REPORTS / "baseline_vector_drcd.json", 100),
    "handwritten": (REPORTS / "baseline_vector_handwritten.json", 20),
}


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    """載入評測腳本。

    走檔案路徑而非 `import scripts.eval_retrieval`：`scripts/` 刻意不是 Python 套件
    （沒有 `__init__.py`），否則 `test_layer_contracts.py` 會要求替一支維運腳本宣告
    import-linter contract。形式沿用 `test_openapi_export.py`。
    """
    assert EVAL_SCRIPT.exists(), f"缺少評測腳本：{EVAL_SCRIPT.relative_to(REPO_ROOT)}"
    spec = importlib.util.spec_from_file_location("_eval_retrieval", EVAL_SCRIPT)
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


def _report(runner: ModuleType, tiny_dataset: tuple[Any, Any], **overrides: Any) -> dict[str, Any]:
    goldenset, corpus = tiny_dataset
    kwargs: dict[str, Any] = {
        "mode": "vector",
        "dataset_name": "tiny",
        "goldenset": goldenset,
        "corpus": corpus,
        "outcomes": [QuestionOutcome("q1", ("p1", "p2"), frozenset({"p1"}))],
        "retrieval": {
            "embedding_provider": "gemini",
            "embedding_model": "text-embedding-004",
            "top_k": 20,
            "params": {"min_score_ratio": 0.0},
        },
    }
    kwargs.update(overrides)
    report = runner.build_report(**kwargs)
    assert isinstance(report, dict)
    return report


class TestReportShape:
    def test_it_carries_everything_needed_to_interpret_the_numbers(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """半年後看到一份報告，要能回答「這是拿什麼、用什麼參數、量什麼題組跑出來的」。
        少了任何一項，那份分數就只是一個沒有上下文的數字。"""
        report = _report(runner, tiny_dataset)

        assert set(report) >= {
            "schema_version",
            "mode",
            "created_at",
            "dataset",
            "retrieval",
            "metrics",
            "per_question",
        }
        assert report["schema_version"] == 1
        assert report["mode"] == "vector"
        assert set(report["dataset"]) >= {
            "name",
            "goldenset_sha256",
            "corpus_sha256",
            "question_count",
            "passage_count",
        }
        assert set(report["retrieval"]) >= {"embedding_provider", "embedding_model", "top_k"}

    def test_the_dataset_fingerprints_come_from_the_files(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        goldenset, corpus = tiny_dataset
        report = _report(runner, tiny_dataset)

        assert report["dataset"]["goldenset_sha256"] == goldenset.sha256
        assert report["dataset"]["corpus_sha256"] == corpus.sha256

    def test_per_question_rows_show_where_the_answer_landed(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """彙總分數只說「幾分」，**逐題結果才說得出哪幾題壞了**——2B-1 之後要靠它看
        FTS 救回了哪些題、又打壞了哪些題。沒有它，改進就只能憑一個總分猜。"""
        rows = _report(runner, tiny_dataset)["per_question"]

        assert len(rows) == 1
        assert set(rows[0]) >= {
            "question_id",
            "hit_rank",
            "retrieved",
            "retrieved_chunk_ids",
            "relevant",
        }
        assert rows[0]["hit_rank"] == 1

    def test_a_missed_question_records_a_null_rank(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """**null 而不是 0 或 -1**：0 在名次的語意裡是「第 0 名」，而排序時 -1 會排到
        最前面——兩者都會讓「哪幾題沒救回來」這個查詢悄悄地回錯答案。"""
        missed = [QuestionOutcome("q1", ("p2",), frozenset({"p1"}))]

        report = _report(runner, tiny_dataset, outcomes=missed)

        assert report["per_question"][0]["hit_rank"] is None

    def test_it_serialises_to_json_without_help(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """報告會被寫成檔案並提交——裡面不能有 dataclass、UUID、set 這類要自訂
        encoder 的東西，否則哪天多一個欄位就在寫檔那一刻才炸。"""
        json.dumps(_report(runner, tiny_dataset), ensure_ascii=False)


class TestModes:
    def test_all_three_modes_are_declared(self, runner: ModuleType) -> None:
        # `vector+rerank` 是 2B-3 加的第四格：沒有它就分不出「hybrid+rerank 贏了」是
        # rerank 的功勞還是 hybrid 的——而 2B-2 的數據顯示 hybrid 目前是負貢獻，這個
        # 歸因問題因此不是假想的。
        assert tuple(runner.MODES) == ("vector", "vector+rerank", "hybrid", "hybrid+rerank")

    def test_every_mode_is_implemented_after_2b4(self, runner: ModuleType) -> None:
        """兩個 rerank 模式在 2B-4 開通（真 TEI + `bge-reranker-v2-m3`）。每開通一個
        模式就改這裡一次——這份清單是「評測現在量得出什麼」的唯一聲明。"""
        assert tuple(runner.IMPLEMENTED_MODES) == tuple(runner.MODES)

    def test_a_mode_that_is_declared_but_missing_still_refuses_to_run(
        self, runner: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**不得偷偷跑成純向量**：那會產生一份標著 hybrid+rerank 而其實沒 rerank 的
        報告，而它與真的長得一模一樣。守門留著，下一個新模式進來時仍然生效。"""
        monkeypatch.setattr(runner, "IMPLEMENTED_MODES", ("vector",))

        with pytest.raises(NotImplementedError):
            runner.validate_mode("hybrid+rerank")

    def test_an_unknown_mode_is_a_plain_error(self, runner: ModuleType) -> None:
        with pytest.raises(ValueError):
            runner.validate_mode("magic")


class TestMockRerankerIsRefused:
    """`--allow-mock` 的第二半：**embedding 是真的、reranker 是 mock** 也量不出品質。

    `MockRerankProvider` 的分數是查詢與段落的字元重疊比例——它驗得了「rerank 有沒有被
    呼叫」，量不了「排得好不好」。拿它跑一份 `hybrid+rerank` 報告，數字看起來完全正常
    （0~1、有排序、有進步或退步），而它衡量的是中文字元的交集大小。那份報告會被拿去
    回答 DoD ②「hybrid 檢索評測優於純向量」。
    """

    def test_a_rerank_mode_needs_a_real_reranker(self, runner: ModuleType) -> None:
        with pytest.raises(runner.EvaluationError):
            runner.require_real_providers(
                "hybrid+rerank",
                embedding_provider="gemini",
                rerank_provider="mock",
                allow_mock=False,
            )

    def test_the_escape_hatch_is_explicit(self, runner: ModuleType) -> None:
        """管線本身（模式接得上、報告寫得出來）仍然要驗得動，所以旗標留著——但它要
        用手打出來，而不是預設。"""
        runner.require_real_providers(
            "hybrid+rerank",
            embedding_provider="mock",
            rerank_provider="mock",
            allow_mock=True,
        )

    def test_a_non_rerank_mode_does_not_care_about_the_reranker(self, runner: ModuleType) -> None:
        """純向量與 hybrid 根本不呼叫 reranker——在那裡要求真 provider 只會擋住
        重跑 baseline 的人，而擋的理由不存在。"""
        runner.require_real_providers(
            "hybrid",
            embedding_provider="gemini",
            rerank_provider="mock",
            allow_mock=False,
        )

    def test_a_mock_embedding_is_still_refused(self, runner: ModuleType) -> None:
        """2B-0 的那一半不變。"""
        with pytest.raises(runner.EvaluationError):
            runner.require_real_providers(
                "vector",
                embedding_provider="mock",
                rerank_provider="tei",
                allow_mock=False,
            )


class TestRerankAttribution:
    """一份 rerank 報告要說得出**是誰排的**。

    半年後桌上有兩份 `hybrid+rerank` 報告、分數差 0.08，而其中一份是 TEI 本機跑的、
    另一份是 Jina 雲端跑的——沒有記下來的話，那個差距會被當成別的改動的功勞。
    """

    def _rerank_report(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any], **extra: Any
    ) -> dict[str, Any]:
        retrieval = {
            "embedding_provider": "gemini",
            "embedding_model": "text-embedding-004",
            "top_k": 20,
            "params": {"min_score_ratio": 0.0},
        }
        retrieval.update(extra)
        return _report(runner, tiny_dataset, mode="hybrid+rerank", retrieval=retrieval)

    def test_a_rerank_report_records_the_reranker(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        report = self._rerank_report(
            runner, tiny_dataset, rerank_provider="tei", rerank_model="BAAI/bge-reranker-v2-m3"
        )

        assert report["retrieval"]["rerank_provider"] == "tei"
        assert report["retrieval"]["rerank_model"] == "BAAI/bge-reranker-v2-m3"

    def test_a_rerank_report_without_a_reranker_is_refused(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """漏填不是小事：那份報告之後仍然可以與別的報告比對（比對只看題組與 embedding
        模型），而它的分數會被歸給錯的東西。"""
        with pytest.raises(runner.EvaluationError):
            self._rerank_report(runner, tiny_dataset)

    def test_a_vector_report_stays_clean(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """沒跑 rerank 的報告不該有 `rerank_model: null` 這種欄位——它讀起來像
        「跑了但沒記」，而那正是上一條要擋的情況。"""
        report = _report(runner, tiny_dataset)

        assert "rerank_provider" not in report["retrieval"]


class TestCompare:
    def _pair(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        baseline = _report(runner, tiny_dataset)
        better = _report(
            runner,
            tiny_dataset,
            outcomes=[
                QuestionOutcome("q1", ("p1", "p2"), frozenset({"p1"})),
                QuestionOutcome("q2", ("p2",), frozenset({"p2"})),
            ],
        )
        return baseline, better

    def test_it_reports_a_delta_for_every_shared_metric(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        baseline, candidate = self._pair(runner, tiny_dataset)

        comparison = runner.compare_reports(baseline, candidate)

        assert set(comparison.deltas) >= {"recall@1", "recall@10", "mrr"}

    def test_it_names_the_metrics_that_decide_the_verdict(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """DoD ②「hybrid 優於純向量」要有**事先講好的**指標。事後挑一個有進步的指標來
        宣布勝利，是評測最容易出現的自欺。

        **`recall@10` 不能當主指標**（2026-08-23 依 2B-0 的 baseline 實測改）：DRCD 在
        純向量下 recall@5 起就是 1.000，那個指標只有退步空間、沒有進步空間，拿它證明
        「hybrid 比較好」在數學上不可能成立。
        """
        baseline, candidate = self._pair(runner, tiny_dataset)

        comparison = runner.compare_reports(baseline, candidate)

        assert comparison.primary == "recall@1"
        assert comparison.secondary == "mrr"
        assert isinstance(comparison.improved, bool)

    def test_a_gain_paid_for_by_a_worse_mrr_is_not_an_improvement(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """主指標上升、次指標下降 → **不算進步**。

        那種形狀的意思是「第一名多對了幾題，其餘題目整體往後掉」——分數被挪到看得見
        的地方而已。只看 recall@1 的話這種交換會被記成勝利，而使用者感受到的是「有時
        候第一筆很準，有時候整段答非所問」。
        """
        # 三題從第二名升到第一名（recall@1 +0.2、rr 各 +0.5），兩題從第一名掉到第六名
        # （recall@1 −0.2、rr 各 −0.833）：recall@1 淨升、mrr 淨降。
        far = ("p9", "p8", "p7", "p6", "p5")
        before = [
            QuestionOutcome("q1", ("p9", "p1"), frozenset({"p1"})),
            QuestionOutcome("q2", ("p9", "p2"), frozenset({"p2"})),
            QuestionOutcome("q3", ("p9", "p3"), frozenset({"p3"})),
            QuestionOutcome("q4", ("p4",), frozenset({"p4"})),
            QuestionOutcome("q5", ("p5x",), frozenset({"p5x"})),
        ]
        after = [
            QuestionOutcome("q1", ("p1",), frozenset({"p1"})),
            QuestionOutcome("q2", ("p2",), frozenset({"p2"})),
            QuestionOutcome("q3", ("p3",), frozenset({"p3"})),
            QuestionOutcome("q4", (*far, "p4"), frozenset({"p4"})),
            QuestionOutcome("q5", (*far, "p5x"), frozenset({"p5x"})),
        ]
        baseline = _report(runner, tiny_dataset, outcomes=before)
        candidate = _report(runner, tiny_dataset, outcomes=after)

        comparison = runner.compare_reports(baseline, candidate)

        assert comparison.candidate > comparison.baseline, "主指標應該是上升的"
        assert comparison.secondary_candidate < comparison.secondary_baseline
        assert comparison.improved is False

    def test_a_worse_candidate_is_not_marked_improved(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        baseline = _report(runner, tiny_dataset)
        worse = _report(
            runner, tiny_dataset, outcomes=[QuestionOutcome("q1", ("p2",), frozenset({"p1"}))]
        )

        assert runner.compare_reports(baseline, worse).improved is False

    def test_reports_from_a_different_goldenset_cannot_be_compared(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any], tmp_path: Path
    ) -> None:
        goldenset, corpus = tiny_dataset
        other_path = tmp_path / "other.jsonl"
        other_path.write_text(
            '{"question_id": "q9", "question": "另一題", "passage_ids": ["p1"], '
            '"language": "zh-Hant", "source": "handwritten"}\n',
            encoding="utf-8",
        )
        baseline = _report(runner, tiny_dataset)
        candidate = _report(runner, (load_goldenset(other_path), corpus))
        assert goldenset.sha256 != load_goldenset(other_path).sha256

        with pytest.raises(runner.IncomparableReportsError):
            runner.compare_reports(baseline, candidate)

    def test_reports_from_a_different_embedding_model_cannot_be_compared(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        """換 embedding 模型等於換一把尺。這個守門攔的是最有可能發生的意外：`.env`
        被改過，而報告看起來完全正常。"""
        baseline = _report(runner, tiny_dataset)
        candidate = _report(
            runner,
            tiny_dataset,
            retrieval={
                "embedding_provider": "openai",
                "embedding_model": "text-embedding-3-small",
                "top_k": 20,
                "params": {},
            },
        )

        with pytest.raises(runner.IncomparableReportsError):
            runner.compare_reports(baseline, candidate)


class TestRerankReportsRemainComparable:
    """2B-4 的每一次比較都是「純向量 baseline vs 加了 rerank 的候選」。

    比對的門檻只有題組與 embedding 模型（`_require_comparable`）——**刻意不含 rerank
    provider**：把它加進去的話，DoD ② 要問的那個問題（rerank 有沒有讓檢索變好）就永遠
    比不出來，因為 baseline 依定義沒有 reranker。
    """

    def test_a_vector_baseline_compares_against_a_rerank_candidate(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        baseline = _report(runner, tiny_dataset)
        candidate = _report(
            runner,
            tiny_dataset,
            mode="hybrid+rerank",
            retrieval={
                "embedding_provider": "gemini",
                "embedding_model": "text-embedding-004",
                "top_k": 20,
                "params": {"min_score_ratio": 0.0},
                "rerank_provider": "tei",
                "rerank_model": "BAAI/bge-reranker-v2-m3",
            },
        )

        comparison = runner.compare_reports(baseline, candidate)

        assert comparison.primary == "recall@1"

    def test_a_different_embedding_model_is_still_refused(
        self, runner: ModuleType, tiny_dataset: tuple[Any, Any]
    ) -> None:
        baseline = _report(runner, tiny_dataset)
        candidate = _report(
            runner,
            tiny_dataset,
            retrieval={
                "embedding_provider": "gemini",
                "embedding_model": "text-embedding-005",
                "top_k": 20,
                "params": {"min_score_ratio": 0.0},
            },
        )

        with pytest.raises(runner.IncomparableReportsError):
            runner.compare_reports(baseline, candidate)


class TestDatasets:
    def test_the_registry_points_at_files_that_exist(self, runner: ModuleType) -> None:
        """腳本用名字選題組（`--dataset drcd`），而名字與檔案的對應只有一份。"""
        assert set(runner.DATASETS) == {"drcd", "handwritten"}
        for corpus_path, questions_path in runner.DATASETS.values():
            assert Path(corpus_path).exists()
            assert Path(questions_path).exists()


class TestCli:
    def test_dataset_is_required(self, runner: ModuleType) -> None:
        with pytest.raises(SystemExit):
            runner.parse_args([])

    def test_defaults_are_the_safe_ones(self, runner: ModuleType) -> None:
        """`top_k` 預設 `None` = 用該 KB 生效中的值（15 §4.1）。在這裡寫一個數字的話，
        評測量到的就不是問答實際用的那組參數。"""
        args = runner.parse_args(["--dataset", "drcd"])

        assert args.mode == "vector"
        assert args.top_k is None
        assert args.dataset == "drcd"


class TestBaseline:
    """2B-0 的 DoD：**改任何檢索程式之前**，純向量的分數已經落檔。"""

    @pytest.mark.parametrize(("name", "minimum"), [(k, v[1]) for k, v in _BASELINES.items()])
    def test_the_baseline_report_is_committed(self, name: str, minimum: int) -> None:
        path = _BASELINES[name][0]
        assert path.exists(), f"缺少 baseline 報告：{path.relative_to(BACKEND_ROOT)}"

        report = json.loads(path.read_text(encoding="utf-8"))

        assert report["mode"] == "vector"
        assert report["dataset"]["name"] == name
        assert report["dataset"]["question_count"] >= minimum

    @pytest.mark.parametrize("name", sorted(_BASELINES))
    def test_the_baseline_was_measured_with_a_real_embedding_model(self, name: str) -> None:
        """**mock 量不出品質**：MockProvider 的向量由 SHA-256 決定，具備決定性與相異性
        但沒有語意相似性。拿 mock 跑出來的 baseline 是一組亂數，而它會安靜地成為 2B
        之後每一次比較的起點。"""
        report = json.loads(_BASELINES[name][0].read_text(encoding="utf-8"))

        assert report["retrieval"]["embedding_provider"] != "mock"

    @pytest.mark.parametrize("name", sorted(_BASELINES))
    def test_the_baseline_still_matches_the_dataset_in_the_repo(
        self, runner: ModuleType, name: str
    ) -> None:
        """題組改過而 baseline 沒重跑 → 之後每一次比較都會被 `compare_reports` 擋下來，
        但那要等到有人跑評測才會發現。在這裡先紅，訊息也講得清楚。"""
        report = json.loads(_BASELINES[name][0].read_text(encoding="utf-8"))
        corpus_path, questions_path = runner.DATASETS[name]

        assert report["dataset"]["goldenset_sha256"] == load_goldenset(questions_path).sha256, (
            f"{name} 題組已變動，baseline 需重跑（make eval-retrieval DATASET={name}）"
        )
        assert report["dataset"]["corpus_sha256"] == load_corpus(corpus_path).sha256, (
            f"{name} 語料已變動，baseline 需重跑（make eval-retrieval DATASET={name}）"
        )


class TestItStaysOutOfTheAutomatedSuites:
    """與 `test_dev_launcher.py::TestProviderVerification` 同一條界線：會打真 API 的東西
    只在人手動執行時跑。"""

    def test_the_target_exists(self) -> None:
        assert "eval-retrieval:" in MAKEFILE

    def test_it_is_not_wired_into_test_lint_or_smoke(self) -> None:
        for target in ("test", "lint", "smoke"):
            match = re.search(rf"^{target}:.*$", MAKEFILE, re.MULTILINE)
            assert match is not None, f"找不到 {target} 目標"
            assert "eval-retrieval" not in match.group(0)

    def test_ci_does_not_run_it(self) -> None:
        for path in (REPO_ROOT / ".github" / "workflows").glob("*.yml"):
            assert "eval-retrieval" not in path.read_text(encoding="utf-8"), (
                f"{path.name} 會打真 API——CI 會開始花錢，也會因為別人的服務中斷而紅"
            )

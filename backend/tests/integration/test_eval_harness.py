"""驗收：評測 harness 真的跑得起來（13 §4 工作包 2B-0）。

`test_eval_runner.py` 驗的是算式與報告形狀（不碰 DB），這一層驗的是**接線**：語料進得了
知識庫、檢索回得來、命中對得回題組的正解。三段其中任何一段錯了，分數都會是一個看起來
合理的低分——而低分在評測裡是最容易被接受的錯誤，因為「檢索本來就沒那麼準」。

**本檔不驗檢索品質**（同 `test_vector_retrieval.py` 的最後一段）：MockProvider 的向量由
SHA-256 決定，具備決定性與相異性，但**沒有語意相似性**——「請假」不會靠近「休假」。
因此這裡唯一的品質斷言是「拿段落原文當問題時，它必須找回自己」：那不量品質，量的是
「查詢向量、寫入向量、過濾條件、passage 對照」四者有沒有接對。真的品質數字要用真的
模型跑 `make eval-retrieval`，那是手動的一步。

**語料一段 = 一個 chunk，不走 chunker**（2B-0 的設計決定）。切塊器會依 token 上限把長
段落切開，於是「正解段落」在 DB 裡變成三個 chunk，recall 的分母是段落數而命中是 chunk
數——兩邊對不齊，分數不再有意義。評測要量的是檢索，不是切塊；切塊策略的評測是另一件事
（3B）。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from ai.gateway import AIGateway
from ai.gateway.providers.mock import MockEmbeddingProvider
from core.tenant import tenant_context
from core.uow import unit_of_work
from rag.goldenset import load_corpus, load_goldenset
from repositories.knowledge import ChunkRepository
from services.knowledge.embedding import EmbeddingService
from services.rag.retrieval import RetrievalService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)

_PASSAGES = (
    ("p1", "請假", "員工請假應於三日前提出申請，並經直屬主管核准。"),
    ("p2", "差旅", "出差旅費以實報實銷為原則，需檢附統一發票。"),
    ("p3", "考核", "年度考核於每年十二月進行，結果影響次年調薪。"),
)


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    import importlib.util
    import sys

    script = Path(__file__).resolve().parents[2] / "scripts" / "eval_retrieval.py"
    assert script.exists(), f"缺少評測腳本：{script}"
    spec = importlib.util.spec_from_file_location("_eval_retrieval_integration", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tenants() -> None:
    for tenant_id, name in ((TENANT_A, "a"), (TENANT_B, "b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=f"tenant-{name}")


@pytest.fixture
def dataset(tmp_path: Path) -> tuple[Any, Any]:
    """語料三段；題組的問句**就是段落原文**（見模組 docstring 的品質斷言說明）。"""
    corpus_path = tmp_path / "corpus.jsonl"
    corpus_path.write_text(
        "\n".join(
            json.dumps({"passage_id": pid, "title": title, "text": text}, ensure_ascii=False)
            for pid, title, text in _PASSAGES
        )
        + "\n",
        encoding="utf-8",
    )
    questions_path = tmp_path / "questions.jsonl"
    questions_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "question_id": f"q-{pid}",
                    "question": text,
                    "passage_ids": [pid],
                    "language": "zh-Hant",
                    "source": "handwritten",
                },
                ensure_ascii=False,
            )
            for pid, _title, text in _PASSAGES
        )
        + "\n",
        encoding="utf-8",
    )
    return load_goldenset(questions_path), load_corpus(corpus_path)


def _gateway() -> AIGateway:
    return AIGateway(embedding_provider=MockEmbeddingProvider(), retry_backoff_seconds=())


def _kb(tenant_id: uuid.UUID) -> uuid.UUID:
    with tenant_scope(tenant_id):
        return uuid.UUID(str(make_knowledge_base(tenant_id=tenant_id, name="eval").id))


def _ingest(
    runner: ModuleType, tenant_id: uuid.UUID, kb_id: uuid.UUID, corpus: Any
) -> dict[Any, str]:
    mapping = runner.ingest_corpus(
        tenant_id=tenant_id,
        kb_id=kb_id,
        corpus=corpus,
        embedding=EmbeddingService(gateway=_gateway()),
    )
    assert isinstance(mapping, dict)
    return mapping


def _run(
    runner: ModuleType,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    dataset: tuple[Any, Any],
    **overrides: Any,
) -> dict[str, Any]:
    goldenset, corpus = dataset
    kwargs: dict[str, Any] = {
        "tenant_id": tenant_id,
        "kb_id": kb_id,
        "goldenset": goldenset,
        "corpus": corpus,
        "mode": "vector",
        "top_k": 10,
        "retrieval": RetrievalService(gateway=_gateway()),
    }
    kwargs.update(overrides)
    report = runner.run_evaluation(**kwargs)
    assert isinstance(report, dict)
    return report


def _chunks(tenant_id: uuid.UUID, kb_id: uuid.UUID) -> list[Any]:
    with tenant_context(tenant_id), unit_of_work():
        return ChunkRepository().for_retrieval(kb_id=kb_id)


class TestIngest:
    def test_each_passage_becomes_exactly_one_chunk(
        self, runner: ModuleType, tenants: None, dataset: tuple[Any, Any]
    ) -> None:
        """段落邊界 = chunk 邊界（見模組 docstring）。"""
        _goldenset, corpus = dataset
        kb_id = _kb(TENANT_A)

        mapping = _ingest(runner, TENANT_A, kb_id, corpus)

        assert len(_chunks(TENANT_A, kb_id)) == len(corpus.passages)
        assert sorted(mapping.values()) == sorted(p.passage_id for p in corpus.passages)

    def test_every_chunk_gets_a_vector(
        self, runner: ModuleType, tenants: None, dataset: tuple[Any, Any]
    ) -> None:
        """沒有向量的 chunk 檢索永遠查不到，而報告上看起來只是「這幾題沒救回來」。"""
        _goldenset, corpus = dataset
        kb_id = _kb(TENANT_A)

        _ingest(runner, TENANT_A, kb_id, corpus)

        report = _run(runner, TENANT_A, kb_id, dataset)
        assert report["metrics"]["hit@1"] == pytest.approx(1.0)

    def test_running_it_twice_does_not_duplicate_anything(
        self, runner: ModuleType, tenants: None, dataset: tuple[Any, Any]
    ) -> None:
        """評測會反覆跑（2B-1／2B-2／2B-4 各一次）。每跑一次就多一份語料的話，同一段
        會在檢索結果裡出現兩次，而 recall 看起來還變好了。"""
        _goldenset, corpus = dataset
        kb_id = _kb(TENANT_A)

        _ingest(runner, TENANT_A, kb_id, corpus)
        _ingest(runner, TENANT_A, kb_id, corpus)

        assert len(_chunks(TENANT_A, kb_id)) == len(corpus.passages)


class TestRun:
    def test_a_passage_can_find_itself(
        self, runner: ModuleType, tenants: None, dataset: tuple[Any, Any]
    ) -> None:
        """**本檔唯一的品質斷言**：問句就是段落原文 → 每題第一名都該是自己。

        它驗的是四件事同時接對：查詢用的模型與寫入用的同一個、過濾條件沒把資料擋掉、
        名次方向沒反（相似度大的在前）、chunk 對得回 passage_id。任何一項錯了，這條就
        會掉到 0 分附近。
        """
        _goldenset, corpus = dataset
        kb_id = _kb(TENANT_A)
        _ingest(runner, TENANT_A, kb_id, corpus)

        report = _run(runner, TENANT_A, kb_id, dataset)

        assert report["metrics"]["recall@1"] == pytest.approx(1.0)
        assert report["metrics"]["mrr"] == pytest.approx(1.0)
        assert [row["hit_rank"] for row in report["per_question"]] == [1, 1, 1]

    def test_it_maps_hits_back_to_passages_without_the_ingest_in_memory(
        self, runner: ModuleType, tenants: None, dataset: tuple[Any, Any]
    ) -> None:
        """灌語料與跑評測是兩次獨立的執行（灌一次、之後每個模式各跑一次）。對照表因此
        必須從 DB 重建得出來，不能只活在 `ingest_corpus` 的回傳值裡。"""
        _goldenset, corpus = dataset
        kb_id = _kb(TENANT_A)
        _ingest(runner, TENANT_A, kb_id, corpus)

        report = _run(runner, TENANT_A, kb_id, dataset)

        retrieved = report["per_question"][0]["retrieved"]
        assert retrieved, "檢索結果沒有對應到任何 passage_id"
        assert set(retrieved) <= {passage.passage_id for passage in corpus.passages}

    def test_the_report_counts_every_question(
        self, runner: ModuleType, tenants: None, dataset: tuple[Any, Any]
    ) -> None:
        """靜默少跑幾題會讓分數變好看（沒救回來的那幾題最容易在中途被例外吃掉）。"""
        goldenset, corpus = dataset
        kb_id = _kb(TENANT_A)
        _ingest(runner, TENANT_A, kb_id, corpus)

        report = _run(runner, TENANT_A, kb_id, dataset)

        assert report["metrics"]["question_count"] == len(goldenset.questions)
        assert len(report["per_question"]) == len(goldenset.questions)

    def test_two_runs_produce_the_same_numbers(
        self, runner: ModuleType, tenants: None, dataset: tuple[Any, Any]
    ) -> None:
        """不可重現的評測沒有比較的價值——差異可能來自改動，也可能來自運氣。"""
        _goldenset, corpus = dataset
        kb_id = _kb(TENANT_A)
        _ingest(runner, TENANT_A, kb_id, corpus)

        first = _run(runner, TENANT_A, kb_id, dataset)
        second = _run(runner, TENANT_A, kb_id, dataset)

        assert first["metrics"] == second["metrics"]
        assert first["per_question"] == second["per_question"]

    def test_a_top_k_larger_than_the_corpus_is_fine(
        self, runner: ModuleType, tenants: None, dataset: tuple[Any, Any]
    ) -> None:
        _goldenset, corpus = dataset
        kb_id = _kb(TENANT_A)
        _ingest(runner, TENANT_A, kb_id, corpus)

        report = _run(runner, TENANT_A, kb_id, dataset, top_k=50)

        assert report["metrics"]["recall@1"] == pytest.approx(1.0)


class TestIsolation:
    def test_the_evaluation_never_sees_another_tenant(
        self, runner: ModuleType, tenants: None, dataset: tuple[Any, Any]
    ) -> None:
        """兩個租戶灌**完全相同**的語料——mock 的向量因此也完全相同，是最容易越界的
        情境。越界時分數不會變差（內容一樣），只有引用指向別人的資料，而評測報告完全
        看不出來；因此這裡直接比對命中的 chunk 屬於誰。
        """
        _goldenset, corpus = dataset
        kb_a, kb_b = _kb(TENANT_A), _kb(TENANT_B)
        _ingest(runner, TENANT_A, kb_a, corpus)
        _ingest(runner, TENANT_B, kb_b, corpus)

        report = _run(runner, TENANT_A, kb_a, dataset)

        own = {str(chunk.id) for chunk in _chunks(TENANT_A, kb_a)}
        others = {str(chunk.id) for chunk in _chunks(TENANT_B, kb_b)}
        hit_chunks = {
            str(chunk_id)
            for row in report["per_question"]
            for chunk_id in row["retrieved_chunk_ids"]
        }
        assert hit_chunks, "報告沒有記下命中的 chunk——這條斷言會變成空集合而永遠通過"
        assert hit_chunks <= own
        assert not hit_chunks & others


class TestWiring:
    def test_an_unknown_tenant_slug_fails_loudly(self, runner: ModuleType, tenants: None) -> None:
        """腳本**不自己建租戶**：評測租戶要能被人看見、被 quota 管、被清掉。悄悄建一個
        的話，正式資料庫裡會多出一個沒有人記得的租戶，而它底下有一整份語料。"""
        with pytest.raises(Exception, match="no-such-tenant"):
            runner.resolve_kb(tenant_slug="no-such-tenant", dataset_name="drcd")

    def test_the_report_is_written_as_readable_json(
        self, runner: ModuleType, tenants: None, dataset: tuple[Any, Any], tmp_path: Path
    ) -> None:
        _goldenset, corpus = dataset
        kb_id = _kb(TENANT_A)
        _ingest(runner, TENANT_A, kb_id, corpus)
        report = _run(runner, TENANT_A, kb_id, dataset)

        path = tmp_path / "report.json"
        runner.write_report(report, path)

        assert json.loads(path.read_text(encoding="utf-8"))["metrics"] == report["metrics"]

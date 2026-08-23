"""離線檢索評測（13 §4 工作包 2B-0；Phase 2 DoD ②「hybrid 檢索評測優於純向量」）。

**這支腳本與 `verify_provider.py` 同一個地位：手動執行、會打真 API、不進 CI。**
評測要有意義就必須用真的 embedding 模型——MockProvider 的向量由 SHA-256 決定，具備
決定性與相異性，但沒有語意相似性（「請假」不會靠近「休假」），拿它跑出來的分數是一組
亂數。而真模型要花錢、會因為別人的服務中斷而失敗，接進 CI 的話紅燈與改動無關，久了就
沒有人看紅燈了。`tests/unit/test_eval_runner.py` 守著這條界線。

用法（一律經 make）::

    make eval-retrieval DATASET=drcd                    # 純向量，寫進 reports/
    make eval-retrieval DATASET=drcd MODE=hybrid        # 2B-2 之後才跑得動

它做四件事：把語料灌進評測租戶的知識庫（冪等）、逐題檢索、算分、寫一份 JSON 報告。
**四件事都不准偷偷變動**，因為報告要跨週比較：

1. **語料一段 = 一個 chunk，不走 chunker**。切塊器會把長段落切開，於是「正解段落」在
   DB 裡變成三個 chunk，recall 的分母是段落數而命中是 chunk 數——兩邊對不齊，分數不再
   有意義。評測量的是檢索，不是切塊（切塊的評測是另一件事，3B）。
2. **chunk 的內容就是段落原文**，標題放 meta 不併進內容。併進去的話量到的就不是題組
   定義的那段文字，而所有分數都會偏移一點點、看起來完全正常。
3. **檢索走 `RetrievalService`**（問答用的同一條路），不是在這裡重寫一份查詢。重寫的話
   評測會量到一條沒有人在用的路徑，而 2B-1／2B-2 的改動出現在這裡的機率是零。
4. **報告帶題組與語料的 sha256**。題組動過之後，舊 baseline 的分數就不是同一把尺量出來
   的，而兩個數字照樣相減得出來（`compare_reports` 是唯一擋得住的東西）。

租戶**不由這支腳本建立**：評測租戶要能被人看見、被 quota 管、被清掉。悄悄建一個的話，
資料庫裡會多出一個沒有人記得的租戶，而它底下有一整份語料。先用
``make demo-tenant DEMO_SLUG=lumina-eval`` 開通。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 直接執行時 sys.path[0] 是 scripts/，`import rag.metrics` 會失敗（pyproject 的
# `pythonpath = ["."]` 只對 pytest 生效）。同 scripts/export_openapi.py。
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# 這支腳本碰 DB，因此要有完整的 Django app registry（repositories 會 import models）。
# `setdefault`：pytest 已經設成 `config.settings.test` 時不覆蓋它，否則測試會連到
# 開發資料庫——那是「測試把開發資料刪掉」這類事故的起點。
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

from config.logging import get_logger  # noqa: E402
from config.settings.app_settings import get_app_settings  # noqa: E402
from core.object_storage import build_document_key  # noqa: E402
from core.tenant import tenant_context  # noqa: E402
from core.uow import unit_of_work  # noqa: E402
from etl.tokens import estimate_tokens  # noqa: E402
from rag.goldenset import (  # noqa: E402
    Corpus,
    GoldenSet,
    GoldenSetError,
    load_corpus,
    load_goldenset,
    validate,
)
from rag.metrics import QuestionOutcome, aggregate  # noqa: E402
from repositories.identity import TenantDirectoryRepository  # noqa: E402
from repositories.knowledge import (  # noqa: E402
    ChunkRepository,
    DocumentRepository,
    KnowledgeBaseRepository,
)
from services.knowledge.embedding import EmbeddingService, model_for  # noqa: E402
from services.rag.retrieval import RetrievalService  # noqa: E402

logger = get_logger(__name__)

EVALUATION_ROOT = BACKEND_ROOT / "evaluation"
REPORTS_ROOT = EVALUATION_ROOT / "reports"

# 名字 → (語料, 題組)。**唯一的對應表**：`--dataset drcd` 與報告裡的 `dataset.name`
# 指的是同一件事，兩份對應遲早會漂。
DATASETS: dict[str, tuple[Path, Path]] = {
    "drcd": (
        EVALUATION_ROOT / "corpus" / "drcd.jsonl",
        EVALUATION_ROOT / "goldenset" / "drcd.jsonl",
    ),
    "handwritten": (
        EVALUATION_ROOT / "corpus" / "lumina_docs.jsonl",
        EVALUATION_ROOT / "goldenset" / "handwritten.jsonl",
    ),
}

# 三個模式先宣告、2B-0 只實作第一個（06 §3.1 的檢索鏈）。
MODES = ("vector", "hybrid", "hybrid+rerank")
IMPLEMENTED_MODES = ("vector",)
_MODE_OWNERS = {"hybrid": "2B-2（RRF 融合）", "hybrid+rerank": "2B-4（TEI reranker）"}

SCHEMA_VERSION = 1
# 報告要回答的是「前幾名裡有沒有」，而不同的 k 回答不同的問題：k=1 是「第一名就對」，
# k=20 是「有沒有進候選集」。rerank 只能重排它拿得到的候選，所以 recall@20 掉下去時
# 2B-4 再怎麼調都救不回來——那是 2B-1／2B-2 的責任範圍。
KS = (1, 5, 10, 20)
# **主指標事先講好**：事後挑一個有進步的指標宣布勝利，是評測最容易出現的自欺。
PRIMARY_METRIC = "recall@10"

DEFAULT_TENANT_SLUG = "lumina-eval"
_EVAL_MIME = "application/x-ndjson"


class EvaluationError(Exception):
    """評測跑不下去（租戶不存在、語料與 DB 對不上…）。

    不掛 `core/exceptions.py` 的業務例外階層：那一套是用來翻成 API 回應碼的，而這裡的
    讀者只有這支 CLI 與它的測試。
    """


class IncomparableReportsError(EvaluationError):
    """兩份報告不是同一把尺量出來的。"""


@dataclass(frozen=True, slots=True)
class Comparison:
    primary: str
    baseline: float
    candidate: float
    deltas: Mapping[str, float]
    improved: bool


# ── 模式 ────────────────────────────────────────────────────────


def validate_mode(mode: str) -> str:
    """**尚未實作的模式要明確拒絕**，不能悄悄跑成純向量。

    悄悄跑的話會產生一份標著 hybrid 而其實是向量的報告，而它與真的 hybrid 報告長得
    一模一樣——那份數字之後會被拿來證明「hybrid 沒有比較好」。
    """
    if mode not in MODES:
        raise ValueError(f"未知的模式 {mode!r}；可用的是 {', '.join(MODES)}")
    if mode not in IMPLEMENTED_MODES:
        raise NotImplementedError(f"模式 {mode!r} 尚未實作，排在 {_MODE_OWNERS[mode]}")
    return mode


# ── 報告 ────────────────────────────────────────────────────────


def build_report(
    *,
    mode: str,
    dataset_name: str,
    goldenset: GoldenSet,
    corpus: Corpus,
    outcomes: Sequence[QuestionOutcome],
    retrieval: Mapping[str, Any],
    per_question: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """逐題結果 → 一份可以提交、可以比對的報告。

    `per_question` 可以由呼叫端提供（`run_evaluation` 會帶上 chunk id 這類 outcome 裡
    沒有的欄位）；沒給時由 outcomes 直接產生，測試與離線分析都用得上。
    """
    validate_mode(mode)
    metrics = aggregate(outcomes, ks=KS).as_dict()
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        # UTC 且帶時區：跨機器比對報告時，沒有時區的時間戳等於沒有時間戳。
        "created_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "name": dataset_name,
            "goldenset_sha256": goldenset.sha256,
            "corpus_sha256": corpus.sha256,
            "question_count": len(goldenset.questions),
            "passage_count": len(corpus.passages),
        },
        "retrieval": dict(retrieval),
        "metrics": metrics,
        "per_question": [dict(row) for row in (per_question or _rows(outcomes))],
    }


def write_report(report: Mapping[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # `ensure_ascii=False`：報告裡有中文問句，逃脫成 \uXXXX 之後人就讀不了了，而
    # 「哪幾題沒救回來」正是這份檔案最常被人打開的理由。
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def compare_reports(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any], *, primary: str = PRIMARY_METRIC
) -> Comparison:
    """兩份報告的差異。**不是同一把尺量出來的就拒絕比較。**

    擋的是最有可能發生的意外：題組補了幾題、`.env` 換過 embedding 模型，而報告看起來
    完全正常。兩個數字照樣相減得出來，差值也照樣「看起來有進步」。
    """
    _require_comparable(baseline, candidate)

    base_metrics = _metrics(baseline)
    cand_metrics = _metrics(candidate)
    for source, metrics in (("baseline", base_metrics), ("candidate", cand_metrics)):
        if primary not in metrics:
            raise IncomparableReportsError(f"{source} 報告沒有主指標 {primary}")

    deltas = {
        key: float(cand_metrics[key]) - float(value)
        for key, value in base_metrics.items()
        # `question_count` 不是指標，它的差值只代表「題數變了」——而那件事已經由
        # `_require_comparable` 判斷過（題組 hash 相同時題數必然相同）。
        if key in cand_metrics and key != "question_count"
    }
    return Comparison(
        primary=primary,
        baseline=float(base_metrics[primary]),
        candidate=float(cand_metrics[primary]),
        deltas=deltas,
        improved=float(cand_metrics[primary]) > float(base_metrics[primary]),
    )


# ── 灌語料 ──────────────────────────────────────────────────────


def resolve_kb(*, tenant_slug: str, dataset_name: str) -> tuple[uuid.UUID, uuid.UUID]:
    """評測租戶的 slug + 題組名 → (tenant_id, kb_id)。知識庫不存在時建立。

    **租戶不建、知識庫才建**：租戶是計費與隔離的單位，悄悄多一個沒有人記得的租戶是
    維運問題；知識庫則是這支腳本自己的工作區，每個題組一個，換題組不必手動開。
    """
    tenant_id = TenantDirectoryRepository().get_active_tenant_id(tenant_slug)
    if tenant_id is None:
        raise EvaluationError(
            f"找不到 active 的評測租戶 {tenant_slug!r}——"
            f"先開通：make demo-tenant DEMO_SLUG={tenant_slug}"
        )

    name = f"eval-{dataset_name}"
    with tenant_context(tenant_id), unit_of_work():
        repository = KnowledgeBaseRepository()
        existing = next((kb for kb in repository.list_all() if kb.name == name), None)
        kb = existing or repository.create(
            name=name,
            description="離線檢索評測用（scripts/eval_retrieval.py）",
        )
        return tenant_id, uuid.UUID(str(kb.id))


def ingest_corpus(
    *,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    corpus: Corpus,
    embedding: EmbeddingService | None = None,
) -> dict[str, str]:
    """語料 → 知識庫的 chunk 與向量；回傳 ``chunk_id → passage_id``。**冪等。**

    冪等在這裡不是潔癖：評測會反覆跑（2B-1／2B-2／2B-4 各一次），而每跑一次就多一份
    語料的話，同一段會在檢索結果裡出現兩次，recall 看起來還變好了。

    冪等的鍵是**語料檔的 sha256**（存進 `documents.content_hash`）：語料變了就是另一份
    文件，語料沒變就重用既有的那份。向量的冪等由 `EmbeddingService` 自己顧（它只算
    還沒有向量的 chunk），所以重跑不會重付一次錢。
    """
    service = embedding or EmbeddingService()
    document_id = _ensure_document(tenant_id=tenant_id, kb_id=kb_id, corpus=corpus)
    service.embed_document(tenant_id, document_id)
    return passage_map(tenant_id=tenant_id, kb_id=kb_id)


def passage_map(*, tenant_id: uuid.UUID, kb_id: uuid.UUID) -> dict[str, str]:
    """從 DB 重建 ``chunk_id → passage_id``。

    **必須從 DB 重建，不能只活在 `ingest_corpus` 的回傳值裡**：灌語料與跑評測是兩次
    獨立的執行（灌一次、之後每個模式各跑一次），而對照表是把檢索命中翻回題組單位的
    唯一途徑。
    """
    with tenant_context(tenant_id), unit_of_work():
        chunks = ChunkRepository().for_retrieval(kb_id=kb_id)
    mapping = {}
    for chunk in chunks:
        passage_id = (chunk.meta or {}).get("passage_id")
        if isinstance(passage_id, str):
            mapping[str(chunk.id)] = passage_id
    return mapping


def _ensure_document(*, tenant_id: uuid.UUID, kb_id: uuid.UUID, corpus: Corpus) -> uuid.UUID:
    with tenant_context(tenant_id), unit_of_work():
        documents = DocumentRepository()
        chunks = ChunkRepository()

        existing = documents.find_by_content_hash(kb_id=kb_id, content_hash=corpus.sha256)
        if existing is not None:
            document_id = uuid.UUID(str(existing.id))
            count = len(chunks.for_retrieval(kb_id=kb_id))
            if count != len(corpus.passages):
                # 上一次跑到一半崩潰才會走到這裡。**不自動修**：chunk 上可能已經掛著
                # 向量（`Embedding.chunk` 是 PROTECT），重寫會在刪除那一步炸在半路，
                # 留下一個更難解釋的狀態。刪掉那份文件重跑是乾淨的一步。
                raise EvaluationError(
                    f"知識庫裡有 {count} 個 chunk，但語料有 {len(corpus.passages)} 段"
                    f"——上一次灌到一半中斷了。刪掉文件 {document_id} 後重跑"
                )
            return document_id

        document_id = uuid.uuid4()
        documents.create(
            kb_id=kb_id,
            document_id=document_id,
            filename=f"{corpus.name}.jsonl",
            mime_type=_EVAL_MIME,
            # 物件其實不存在（語料直接進 DB，沒有經過上傳）。key 仍照慣例組出來：
            # 形狀不一致的 storage_key 會持久化，而清理與稽核都以那個前綴認人。
            storage_key=build_document_key(kb_id=kb_id, document_id=document_id),
            content_hash=corpus.sha256,
            size_bytes=sum(len(passage.text.encode()) for passage in corpus.passages),
            source_type="evaluation",
        )
        chunks.replace_for_version(
            document_id=document_id,
            kb_id=kb_id,
            doc_version=1,
            rows=[
                {
                    "seq": index,
                    # 內容就是段落原文（見模組 docstring 第 2 點）。
                    "content": passage.text,
                    "token_count": estimate_tokens(passage.text),
                    "meta": {"passage_id": passage.passage_id, "title": passage.title},
                }
                for index, passage in enumerate(corpus.passages)
            ],
        )
        # `chunked` 是 embedding 的入口狀態（08 §2）。少了它 `embed_document` 會安靜地
        # 跳過整份文件，而症狀是「每一題都零分」。
        documents.set_status(document_id, status="chunked", error=None)
        return document_id


# ── 跑評測 ──────────────────────────────────────────────────────


def run_evaluation(
    *,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    goldenset: GoldenSet,
    corpus: Corpus,
    mode: str,
    top_k: int | None = None,
    retrieval: RetrievalService | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """逐題檢索 → 一份報告。"""
    validate_mode(mode)
    service = retrieval or RetrievalService()
    params = service.params_for(tenant_id, [kb_id])
    effective_top_k = params.top_k if top_k is None else top_k
    mapping = passage_map(tenant_id=tenant_id, kb_id=kb_id)

    questions = goldenset.questions[:limit] if limit else goldenset.questions
    outcomes: list[QuestionOutcome] = []
    rows: list[dict[str, Any]] = []
    for question in questions:
        hits = service.query(tenant_id, kb_id=kb_id, query=question.question, top_k=effective_top_k)
        # **chunk id 全部記下，包含對不回 passage 的那些**：對不回來的命中若被丟掉，
        # 「檢索回了不屬於這個語料的東西」這件事就會從報告裡消失，而那正是租戶隔離
        # 出問題時唯一看得見的痕跡。
        chunk_ids = [str(hit.chunk_id) for hit in hits]
        retrieved = tuple(mapping[chunk_id] for chunk_id in chunk_ids if chunk_id in mapping)
        if len(retrieved) != len(chunk_ids):
            logger.warning(
                "eval_hit_outside_corpus",
                question_id=question.question_id,
                hit_count=len(chunk_ids),
                mapped=len(retrieved),
            )

        outcome = QuestionOutcome(
            question_id=question.question_id,
            retrieved=retrieved,
            relevant=question.passage_ids,
        )
        outcomes.append(outcome)
        rows.append(
            {
                "question_id": question.question_id,
                "hit_rank": _hit_rank(outcome),
                "retrieved": list(retrieved),
                "retrieved_chunk_ids": chunk_ids,
                "relevant": sorted(question.passage_ids),
            }
        )

    with tenant_context(tenant_id), unit_of_work():
        kb = KnowledgeBaseRepository().get_by_id(kb_id)
    settings = get_app_settings()
    return build_report(
        mode=mode,
        # 題組決定名字，不是語料：`handwritten` 的語料檔叫 `lumina_docs`，兩者對得起來
        # 的只有題組那一邊（`DATASETS` 的鍵即是題組檔名）。
        dataset_name=goldenset.name,
        goldenset=goldenset,
        corpus=corpus,
        outcomes=outcomes,
        retrieval={
            "embedding_provider": settings.ai_embedding_provider,
            # KB 的模型優先（06 §2.2：向量只在同一個模型的空間裡可比較），與檢索實際
            # 用的是同一個解析（`services/knowledge/embedding.model_for`）。
            "embedding_model": model_for(str(kb.embedding_model) if kb else ""),
            "top_k": effective_top_k,
            "params": dataclasses.asdict(params),
        },
        per_question=rows,
    )


# ── CLI ────────────────────────────────────────────────────────


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="離線檢索評測（手動執行，會打真 API）")
    parser.add_argument("--dataset", required=True, choices=sorted(DATASETS))
    parser.add_argument("--mode", default="vector", choices=MODES)
    # 預設 `None` = 用該 KB 生效中的值（15 §4.1）。在這裡寫一個數字的話，評測量到的
    # 就不是問答實際用的那組參數，而兩邊的差距不會出現在任何地方。
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--tenant", default=DEFAULT_TENANT_SLUG)
    parser.add_argument("--out", type=Path, default=None, help="預設寫進 evaluation/reports/")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 題（驗管線用）")
    parser.add_argument("--baseline", type=Path, default=None, help="與這份報告比對")
    parser.add_argument(
        "--allow-mock",
        action="store_true",
        help="允許用 mock provider 跑（只驗管線；分數沒有意義）",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_mode(args.mode)
        settings = get_app_settings()
        if settings.ai_embedding_provider == "mock" and not args.allow_mock:
            # 擋在最前面：mock 跑得完、報告長得一模一樣，而分數是亂數。它會安靜地
            # 成為之後每一次比較的起點。
            raise EvaluationError(
                "AI_EMBEDDING_PROVIDER=mock 量不出檢索品質（向量沒有語意相似性）；"
                "設定真 provider，或加 --allow-mock 只驗管線"
            )

        corpus_path, questions_path = DATASETS[args.dataset]
        corpus = load_corpus(corpus_path)
        goldenset = load_goldenset(questions_path)
        validate(goldenset, corpus)

        tenant_id, kb_id = resolve_kb(tenant_slug=args.tenant, dataset_name=args.dataset)
        ingest_corpus(tenant_id=tenant_id, kb_id=kb_id, corpus=corpus)
        report = run_evaluation(
            tenant_id=tenant_id,
            kb_id=kb_id,
            goldenset=goldenset,
            corpus=corpus,
            mode=args.mode,
            top_k=args.top_k,
            limit=args.limit,
        )
    except (EvaluationError, GoldenSetError, NotImplementedError, ValueError) as exc:
        print(f"評測中止：{exc}", file=sys.stderr)
        return 1

    path = write_report(report, args.out or _default_out(args.mode, args.dataset))
    _print_summary(report, path)

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        try:
            _print_comparison(compare_reports(baseline, report))
        except IncomparableReportsError as exc:
            print(f"無法與 baseline 比較：{exc}", file=sys.stderr)
            return 1
    return 0


def _default_out(mode: str, dataset: str) -> Path:
    # `+` 在檔名裡不好打也不好 glob；模式名裡唯一的特殊字元就它。
    return REPORTS_ROOT / f"{mode.replace('+', '_')}_{dataset}.json"


def _print_summary(report: Mapping[str, Any], path: Path) -> None:
    metrics = _metrics(report)
    print(
        f"模式 {report['mode']}／題組 {report['dataset']['name']}"
        f"（{report['dataset']['question_count']} 題 / "
        f"{report['dataset']['passage_count']} 段）"
    )
    print(
        f"  模型 {report['retrieval']['embedding_model']}"
        f"（{report['retrieval']['embedding_provider']}）"
        f"　top_k={report['retrieval']['top_k']}"
    )
    for key in sorted(metrics):
        if key != "question_count":
            print(f"  {key:<12} {metrics[key]:.4f}")
    print(f"  → {path}")


def _print_comparison(comparison: Comparison) -> None:
    verdict = "優於" if comparison.improved else "未優於"
    print(
        f"\n對照 baseline：{comparison.primary} "
        f"{comparison.baseline:.4f} → {comparison.candidate:.4f}"
        f"（{verdict} baseline）"
    )
    for key in sorted(comparison.deltas):
        print(f"  {key:<12} {comparison.deltas[key]:+.4f}")


# ── 內部 ────────────────────────────────────────────────────────


def _rows(outcomes: Sequence[QuestionOutcome]) -> list[dict[str, Any]]:
    return [
        {
            "question_id": outcome.question_id,
            "hit_rank": _hit_rank(outcome),
            "retrieved": list(outcome.retrieved),
            "retrieved_chunk_ids": [],
            "relevant": sorted(outcome.relevant),
        }
        for outcome in outcomes
    ]


def _hit_rank(outcome: QuestionOutcome) -> int | None:
    """第一個命中的名次（從 1 起算），沒命中回 `None`。

    **`None` 而不是 0 或 -1**：0 在名次的語意裡是「第 0 名」，而排序時 -1 會排到最前面
    ——兩者都會讓「哪幾題沒救回來」這個查詢悄悄地回錯答案。去重後計算，與
    `rag/metrics.py` 的名次是同一套（同一段被兩路命中不該讓後面的東西整體往後挪）。
    """
    for index, passage_id in enumerate(outcome.top(max(len(outcome.retrieved), 1)), start=1):
        if passage_id in outcome.relevant:
            return index
    return None


def _metrics(report: Mapping[str, Any]) -> Mapping[str, float]:
    metrics = report.get("metrics")
    if not isinstance(metrics, Mapping):
        raise IncomparableReportsError("報告裡沒有 metrics 區塊")
    return metrics


def _require_comparable(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
    differences = []
    for section, keys in (
        ("dataset", ("name", "goldenset_sha256", "corpus_sha256")),
        ("retrieval", ("embedding_model",)),
    ):
        left = baseline.get(section) or {}
        right = candidate.get(section) or {}
        differences += [
            f"{section}.{key}（{left.get(key)!r} ≠ {right.get(key)!r}）"
            for key in keys
            if left.get(key) != right.get(key)
        ]
    if differences:
        raise IncomparableReportsError("兩份報告不是同一把尺量出來的：" + "；".join(differences))


if __name__ == "__main__":
    raise SystemExit(main())

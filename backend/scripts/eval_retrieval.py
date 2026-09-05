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
import math
import os
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

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
    EmbeddingRepository,
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

# 四個模式先宣告，實作逐包開通（06 §3.1 的檢索鏈）。
#
# **2B-4 全部開通**：真的 reranker（自架 TEI 的 `bge-reranker-v2-m3`）接上之後，
# 兩個 rerank 模式才量得出品質——2B-3 的 `MockRerankProvider` 打的是字元重疊比例，
# 拿它跑評測量到的是亂數，而報告看起來完全正常（守門改由 `require_real_providers`
# 負責，見下）。
#
# `vector+rerank` 這一格的存在理由是**歸因**：少了它，`hybrid+rerank` 贏了也分不出是
# rerank 的功勞還是 hybrid 的，而 2B-2 的數據顯示 hybrid 在沒有裁判時是負貢獻。
#
# 清單留著而不是刪掉：它是「評測現在量得出什麼」的唯一聲明，下一個新模式進來時
# `validate_mode` 仍然擋得住「偷偷跑成純向量」。
MODES = ("vector", "vector+rerank", "hybrid", "hybrid+rerank")
IMPLEMENTED_MODES = MODES
_MODE_OWNERS: dict[str, str] = {}

# 2B-5 起是 2：逐題多記 `scores`（cross-encoder 給的分數）與整份的 `rerank_scores`
# 分布。**純新增欄位**，所以 2B-0 的 baseline（version 1）仍然比得動——可比性看的是
# 題組與 embedding 模型（`_require_comparable`），不是報告的版本。版本仍要動，因為
# 讀報告的人要分得出「這一份沒有分數」是因為它是舊的，而不是因為那次跑掉了。
#
# **001-eval-rebaseline 起是 3**：`retrieval.embedding_dimensions` 成為必填。這一次
# 與上一次不同——上一次是純新增，舊報告照樣比得動；這一次新欄位**進了可比性判斷**，
# 因此 version 2 的報告在任何比較裡都會被拒絕。那是刻意的：它們是 1536 維量出來的，
# 而 W1 之後的報告是 1024 維，兩者的 `embedding_model` 卻可以完全相同。
SCHEMA_VERSION = 3
# 報告要回答的是「前幾名裡有沒有」，而不同的 k 回答不同的問題：k=1 是「第一名就對」，
# k=20 是「有沒有進候選集」。rerank 只能重排它拿得到的候選，所以 recall@20 掉下去時
# 2B-4 再怎麼調都救不回來——那是 2B-1／2B-2 的責任範圍。
KS = (1, 5, 10, 20)
# **主指標事先講好**：事後挑一個有進步的指標宣布勝利，是評測最容易出現的自欺。
#
# **2026-08-23 由 `recall@10` 改為 `recall@1` + `mrr`，依據是 2B-0 的 baseline 實測**：
# DRCD 題組在純向量下 recall@5 起就是 1.000（120 題有 113 題排第一，最差名次 3），
# 也就是原本的主指標**只有退步空間、沒有進步空間**——拿它證明「hybrid 優於純向量」
# 在數學上不可能成立。recall@1 與 mrr 兩邊都還有空間（DRCD 0.9417／0.9653，自家文件
# 的手寫題組 0.4375／0.6046）。
#
# **兩個指標一起看，而不是只換一個**：recall@1 上升而 mrr 下降的意思是「第一名多對了
# 幾題，其餘題目整體往後掉」——那不是進步，是把分數挪到看得見的地方。因此判定規則是
# 主指標上升**且**次指標不退步。
PRIMARY_METRIC = "recall@1"
SECONDARY_METRIC = "mrr"

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
    secondary: str
    baseline: float
    candidate: float
    secondary_baseline: float
    secondary_candidate: float
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
        owner = _MODE_OWNERS.get(mode, "尚未排定的工作包")
        raise NotImplementedError(f"模式 {mode!r} 尚未實作，排在 {owner}")
    return mode


def require_real_providers(
    mode: str, *, embedding_provider: str, rerank_provider: str, allow_mock: bool
) -> None:
    """**mock 量不出品質**——兩條路各擋一次。

    embedding 那半是 2B-0 立的：MockProvider 的向量由 SHA-256 決定，有決定性也有相異
    性，就是沒有語意相似性。

    rerank 這半是 2B-4 補的，而它更容易漏：跑 `hybrid+rerank` 的人通常已經設好了真的
    embedding 金鑰，於是第一道關卡放行，而 `AI_RERANK_PROVIDER` 還停在預設的 `mock`
    ——那份報告會有排序、有分數、有進退步，衡量的卻是查詢與段落的中文字元交集大小。
    然後它會被拿去回答 DoD ②「hybrid 檢索評測優於純向量」。

    `--allow-mock` 留著是因為**管線本身**（模式接得上、報告寫得出來）仍然要驗得動，
    但它要用手打出來。
    """
    if allow_mock:
        return
    if embedding_provider == "mock":
        raise EvaluationError(
            "AI_EMBEDDING_PROVIDER=mock 量不出檢索品質（向量沒有語意相似性）；"
            "設定真 provider，或加 --allow-mock 只驗管線"
        )
    # 沒跑 rerank 的模式不呼叫 reranker——在那裡要求真 provider 只會擋住重跑 baseline
    # 的人，而擋的理由不存在。
    if mode.endswith("+rerank") and rerank_provider == "mock":
        raise EvaluationError(
            f"模式 {mode} 需要真的 reranker，但 AI_RERANK_PROVIDER=mock"
            "（假分數是字元重疊比例，量不出排序品質）；"
            "先 make tei-up 並設 AI_RERANK_PROVIDER=tei，或加 --allow-mock 只驗管線"
        )


# ── 維度 ────────────────────────────────────────────────────────

# 探幾個 chunk 才算「這個 KB 沒有向量」。取 1 不夠：上一次跑到一半中斷時，第一個
# chunk 可能剛好是沒算到的那個，而整份報告會因此拒絕產出。取全部則會把 1,200 條
# 1024 維的向量整批撈進記憶體，只為了量其中一條的長度。
_DIMENSION_PROBE_CHUNKS = 32


class _EmbeddingSource(Protocol):
    """`resolve_embedding_dimensions` 需要的最小介面。

    宣告成 Protocol 而不是直接吃 `EmbeddingRepository`：維度解析本身是一段純粹的
    「量一下這條向量多長」，不該為了測它而需要一個資料庫。
    """

    def first_vector_for_kb(
        self, *, kb_id: Any, model: str, embedding_version: int
    ) -> Sequence[float] | None: ...


def resolve_embedding_dimensions(
    *, embeddings: _EmbeddingSource, kb_id: Any, model: str, embedding_version: int
) -> int:
    """報告要記的維度＝**實際存下來的向量長度**，不是 `AI_EMBEDDING_DIMENSIONS`。

    設定值是「要求的維度」。`VendorSpec.supports_dimensions` 為 False 的那幾家
    （`tei`／`vllm`／`nvidia`）根本不會把 `dimensions` 送出去，於是設定填什麼都不影響
    回來的向量——記設定值等於在報告裡放一個看起來精確、而可能整份都不成立的數字。

    **查不到向量時不准猜**。那正是 W1 之後、reindex 之前的狀態：chunk 都在、文件狀態
    是 `ready`，而向量是空的。此時退回設定值會產出一份標著 1024 維、實際上一筆向量都
    沒查到的報告，而它的每一題都會是零分——讀報告的人會以為是檢索變差了。
    """
    vector = embeddings.first_vector_for_kb(
        kb_id=kb_id, model=model, embedding_version=embedding_version
    )
    if not vector:
        raise EvaluationError(
            f"知識庫 {kb_id} 底下找不到 {model} v{embedding_version} 的向量，"
            "量不出維度——先確認語料已嵌入（W1 的 migration 清空過向量）"
        )
    return len(vector)


@dataclass(frozen=True, slots=True)
class _EvalEmbeddings:
    """把評測租戶的向量查詢包成 `_EmbeddingSource`。

    存在的理由只有一個：`EmbeddingRepository` 沒有「給我這個 KB 的任一條向量」這個
    方法，而**為了量一次維度去改 repository 層並不划算**——那一層是 API 與 worker
    共用的，多一個只有離線腳本用得到的方法，下一個讀它的人得先確認沒有別人在用。
    """

    tenant_id: uuid.UUID

    def first_vector_for_kb(
        self, *, kb_id: Any, model: str, embedding_version: int
    ) -> Sequence[float] | None:
        with tenant_context(self.tenant_id), unit_of_work():
            chunks = ChunkRepository().for_retrieval(kb_id=kb_id)[:_DIMENSION_PROBE_CHUNKS]
            if not chunks:
                return None
            rows = EmbeddingRepository().for_chunks(
                [chunk.id for chunk in chunks],
                model=model,
                embedding_version=embedding_version,
            )
        return list(rows[0].vector) if rows else None


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
    if "embedding_dimensions" not in retrieval:
        # **模型名稱不足以認出一把尺**：W1 之後同一家雲端模型可以跑在 1024 維（它支援
        # Matryoshka 截斷），而 W1 之前的報告是 1536 維的——兩份的 `embedding_model`
        # 完全相同。少了維度，`_require_comparable` 會判定它們是同一把尺並放行相減。
        #
        # 擋在產出這一步而不是比較那一步：報告寫進磁碟就會被提交，等到有人拿它來比較
        # 才發現少一欄時，「當時跑在幾維」已經補不回來了。
        raise EvaluationError("報告必須記下 retrieval.embedding_dimensions（模型名稱認不出維度）")
    if mode.endswith("+rerank") and not {"rerank_provider", "rerank_model"} <= set(retrieval):
        # **漏填不是小事**：報告的比對門檻只看題組與 embedding 模型（`_require_comparable`
        # 的理由見那裡），所以一份沒記 reranker 的 rerank 報告照樣比得動——它的分數
        # 之後會被歸給錯的東西。半年後桌上兩份 `hybrid+rerank` 差 0.08，一份是本機 TEI
        # 跑的、一份是 Jina 跑的，沒記下來就看不出來。
        raise EvaluationError(f"模式 {mode} 的報告必須記下 rerank_provider 與 rerank_model")
    rows = [dict(row) for row in (per_question or _rows(outcomes))]
    if mode.endswith("+rerank"):
        _require_scores(rows)
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
        # 只有真的跑了 rerank 才有這一段（同 `rerank_model` 的處置）：純向量報告掛一個
        # 空的 `rerank_scores` 讀起來像「跑了但沒記」。
        **({"rerank_scores": _score_distribution(rows)} if mode.endswith("+rerank") else {}),
        "per_question": rows,
    }


def write_report(report: Mapping[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # `ensure_ascii=False`：報告裡有中文問句，逃脫成 \uXXXX 之後人就讀不了了，而
    # 「哪幾題沒救回來」正是這份檔案最常被人打開的理由。
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def compare_reports(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    primary: str = PRIMARY_METRIC,
    secondary: str = SECONDARY_METRIC,
) -> Comparison:
    """兩份報告的差異。**不是同一把尺量出來的就拒絕比較。**

    擋的是最有可能發生的意外：題組補了幾題、`.env` 換過 embedding 模型，而報告看起來
    完全正常。兩個數字照樣相減得出來，差值也照樣「看起來有進步」。

    `improved` 的規則見 `PRIMARY_METRIC` 上方的說明：主指標上升**且**次指標不退步。
    """
    _require_comparable(baseline, candidate)

    base_metrics = _metrics(baseline)
    cand_metrics = _metrics(candidate)
    for source, metrics in (("baseline", base_metrics), ("candidate", cand_metrics)):
        for role, key in (("主指標", primary), ("次指標", secondary)):
            if key not in metrics:
                raise IncomparableReportsError(f"{source} 報告沒有{role} {key}")

    deltas = {
        key: float(cand_metrics[key]) - float(value)
        for key, value in base_metrics.items()
        # `question_count` 不是指標，它的差值只代表「題數變了」——而那件事已經由
        # `_require_comparable` 判斷過（題組 hash 相同時題數必然相同）。
        if key in cand_metrics and key != "question_count"
    }
    return Comparison(
        primary=primary,
        secondary=secondary,
        baseline=float(base_metrics[primary]),
        candidate=float(cand_metrics[primary]),
        secondary_baseline=float(base_metrics[secondary]),
        secondary_candidate=float(cand_metrics[secondary]),
        deltas=deltas,
        improved=(
            float(cand_metrics[primary]) > float(base_metrics[primary])
            and float(cand_metrics[secondary]) >= float(base_metrics[secondary])
        ),
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
        retrieved_outcome = service.query(
            tenant_id,
            kb_id=kb_id,
            query=question.question,
            top_k=effective_top_k,
            # **模式由報告說了算，不是由 KB 的設定說了算**：評測要在同一份資料上量出
            # 兩種模式的差距，而 KB config 是「正式路徑用哪一種」。不覆寫的話，
            # `--mode vector` 會安靜地跑成 hybrid，而 2B-2 的結論就是拿 hybrid 跟
            # hybrid 比——差距是 0，報告看起來完全正常。
            mode=mode,
        )
        hits = retrieved_outcome.chunks
        # **chunk id 全部記下，包含對不回 passage 的那些**：對不回來的命中若被丟掉，
        # 「檢索回了不屬於這個語料的東西」這件事就會從報告裡消失，而那正是租戶隔離
        # 出問題時唯一看得見的痕跡。
        chunk_ids = [str(hit.chunk_id) for hit in hits]
        # **段落與分數在同一個推導式裡產生**，因此逐一對得起來。分開兩次過濾的話，
        # 「對不回語料的那幾段」會讓兩份清單錯位，而「正解拿了幾分」會取到隔壁那一段
        # 的分數——報告看起來完全正常，分布也很漂亮（`_require_scores` 擋長度，
        # 這裡擋的是順序）。
        mapped = [
            (mapping[chunk_id], hit.score)
            for chunk_id, hit in zip(chunk_ids, hits, strict=True)
            if chunk_id in mapping
        ]
        retrieved = tuple(passage_id for passage_id, _ in mapped)
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
                # rerank 模式下這就是 cross-encoder 給的 0~1 分數（`RetrievalOutcome`
                # 的 `score` 在 rerank 之後已經換成它）。非 rerank 模式記的是融合
                # 分數，`build_report` 不會拿它去產分布——那兩個尺度不能混。
                "scores": [round(score, 6) for _, score in mapped],
            }
        )

    with tenant_context(tenant_id), unit_of_work():
        kb = KnowledgeBaseRepository().get_by_id(kb_id)
    settings = get_app_settings()
    # KB 的模型優先（06 §2.2：向量只在同一個模型的空間裡可比較），與檢索實際用的是
    # 同一個解析（`services/knowledge/embedding.model_for`）。
    embedding_model = model_for(str(kb.embedding_model) if kb else "")
    embedding_version = int(kb.embedding_version) if kb else 1
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
            "embedding_model": embedding_model,
            # **量出來的，不是設定的**（見 `resolve_embedding_dimensions`）。
            "embedding_dimensions": resolve_embedding_dimensions(
                embeddings=_EvalEmbeddings(tenant_id=tenant_id),
                kb_id=kb_id,
                model=embedding_model,
                embedding_version=embedding_version,
            ),
            "top_k": effective_top_k,
            # 報告要記**實際生效**的模式，不是 KB 設定的那個（見上方 `mode=mode`）。
            "mode": mode,
            "params": dataclasses.asdict(params),
            # 只有真的跑了 rerank 才記。純向量報告掛一個 `rerank_model: null` 讀起來
            # 像「跑了但沒記」，而那正是 `build_report` 要擋的情況。
            **(
                {
                    "rerank_provider": settings.ai_rerank_provider,
                    "rerank_model": settings.ai_rerank_model,
                }
                if mode.endswith("+rerank")
                else {}
            ),
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
        # 擋在最前面：mock 跑得完、報告長得一模一樣，而分數是亂數。它會安靜地
        # 成為之後每一次比較的起點。
        require_real_providers(
            args.mode,
            embedding_provider=settings.ai_embedding_provider,
            rerank_provider=settings.ai_rerank_provider,
            allow_mock=args.allow_mock,
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

    # 模型取自報告本身而不是設定：報告記的是**實際生效**的那個，而環境變數與
    # `--env-file` 的優先順序不是每個人都記得（切錯時檔名會跟著錯，於是覆蓋掉別人）。
    model = str(report["retrieval"]["embedding_model"])
    path = write_report(report, args.out or _default_out(args.mode, args.dataset, model))
    _print_summary(report, path)

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        try:
            _print_comparison(compare_reports(baseline, report))
        except IncomparableReportsError as exc:
            print(f"無法與 baseline 比較：{exc}", file=sys.stderr)
            return 1
    return 0


def _model_slug(model: str) -> str:
    """模型名 → 一段可以當目錄名的字串。

    `BAAI/bge-m3` 帶著斜線，直接拼進路徑會多長出一層目錄——報告還是寫得出來，而下一個
    人照著文件的路徑去找會找不到，然後以為那一次沒跑。`:`（`org/model:tag`）與空白
    同理。**換成 `_` 而不是砍掉前綴**：`bge-m3` 丟掉了「哪一家的」，而那正是跨模型
    比較要分辨的東西。
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", model).strip("_")
    return slug or "unknown-model"


def _default_out(mode: str, dataset: str, model: str) -> Path:
    """`reports/<model>/<mode>_<dataset>.json`。

    **模型必須進路徑**：舊的 `<mode>_<dataset>.json` 在兩個模型跑同一個模式時會互相
    覆蓋，而覆蓋沒有任何徵兆——檔案還在、內容合法、時間戳是新的。001-eval-rebaseline
    要在同一份題組上跑兩個模型各四個模式，這是一定會撞上的。
    """
    # `+` 在檔名裡不好打也不好 glob；模式名裡唯一的特殊字元就它。
    return REPORTS_ROOT / _model_slug(model) / f"{mode.replace('+', '_')}_{dataset}.json"


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
    _print_score_distribution(report)
    print(f"  → {path}")


def _print_score_distribution(report: Mapping[str, Any]) -> None:
    """裁決絕對門檻要看的兩個數字，直接印出來（2B-4 結案缺口①）。

    要人自己去翻 JSON 的話，這份資料就只會在「有人特地想起它」的那一天被用到——
    而缺口①已經帶過一整個工作包了。
    """
    distribution = report.get("rerank_scores")
    if not isinstance(distribution, Mapping):
        return
    print("  rerank 分數分布（裁決絕對門檻用）：")
    for group, label in (("hit", "正解  "), ("miss", "非正解")):
        summary = distribution.get(group) or {}
        if not summary.get("count"):
            print(f"    {label} 無樣本")
            continue
        print(
            f"    {label} n={summary['count']:<4} "
            f"min={summary['min']:.4f} p05={summary['p05']:.4f} "
            f"p50={summary['p50']:.4f} p95={summary['p95']:.4f} max={summary['max']:.4f}"
        )
    hit = distribution.get("hit") or {}
    miss = distribution.get("miss") or {}
    if hit.get("p05") is not None and miss.get("p95") is not None:
        # 兩端之間有間隙才存在「砍得掉錯的、又留得住對的」那個數字。負的間隙代表
        # **任何**門檻都會付出代價，而那本身就是一個結論。
        print(f"    可用區間 {miss['p95']:.4f} ~ {hit['p05']:.4f}（正解 p05 − 非正解 p95）")


def _print_comparison(comparison: Comparison) -> None:
    verdict = "優於" if comparison.improved else "未優於"
    print(
        f"\n對照 baseline（{verdict} baseline）："
        f"{comparison.primary} {comparison.baseline:.4f} → {comparison.candidate:.4f}；"
        f"{comparison.secondary} {comparison.secondary_baseline:.4f} → "
        f"{comparison.secondary_candidate:.4f}"
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


def _require_scores(rows: Sequence[Mapping[str, Any]]) -> None:
    """rerank 模式的報告**必須**逐題帶著分數（2B-4 結案缺口①）。

    絕對門檻 0.3 至今預設關閉，理由不是「不想開」，是「沒有分布可以裁決它」——而
    一份沒有分數的 rerank 報告照樣比得動、照樣有 recall，半年後要裁決門檻時才發現
    這一份沒得用，那時 GPU 上跑的模型已經換過了。同 `rerank_model` 的漏填處置。

    長度也要對得起來：兩份清單錯位的話，「正解拿了幾分」會取到隔壁那一段的分數
    ——而報告看起來完全正常，分布也很漂亮。
    """
    for row in rows:
        scores = row.get("scores")
        if scores is None:
            raise EvaluationError(
                f"rerank 模式的報告必須逐題記下 rerank 分數（{row.get('question_id')} 沒有）"
            )
        if len(list(scores)) != len(list(row.get("retrieved") or ())):
            raise EvaluationError(
                f"分數與命中的段落數對不起來（{row.get('question_id')}："
                f"{len(list(scores))} 個分數 / {len(list(row.get('retrieved') or ()))} 段）"
            )


def _score_distribution(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """分數分布，**正解與非正解分開**（2B-4 結案缺口①）。

    門檻要回答的問題是「有沒有一個數字砍得掉錯的、又留得住對的」。混在一起的分布
    只說得出「大部分候選分數很低」——那句話對任何一個門檻都成立，於是 0.3 會被一個
    看似漂亮的分布背書，而它同時砍掉了一成正解。那一成的症狀是「這個知識庫對某些
    問題突然說不知道」，沒有任何地方看得出來。

    **沒命中的題目只餵 miss 那一組**：把它記成 hit=0 會把正解的低分端整個拉下來。
    """
    hits: list[float] = []
    misses: list[float] = []
    for row in rows:
        relevant = set(row.get("relevant") or ())
        for passage_id, score in zip(
            row.get("retrieved") or (), row.get("scores") or (), strict=False
        ):
            (hits if passage_id in relevant else misses).append(float(score))
    return {"hit": _summarise(hits), "miss": _summarise(misses)}


def _summarise(values: Sequence[float]) -> dict[str, Any]:
    """裁決門檻要看的是**兩端**：正解的低分端（門檻高於它就會誤砍）與非正解的高分端
    （門檻低於它就等於沒砍）。平均數在這裡毫無用處——它被中間那一大坨拉著走。"""
    if not values:
        # 空的時候其餘欄位是 `None` 而不是 0：0 是一個分數，而「沒有樣本」不是。
        return {
            "count": 0,
            "min": None,
            "p05": None,
            "p25": None,
            "p50": None,
            "p75": None,
            "p95": None,
            "max": None,
        }
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 6),
        "p05": _percentile(ordered, 0.05),
        "p25": _percentile(ordered, 0.25),
        "p50": _percentile(ordered, 0.50),
        "p75": _percentile(ordered, 0.75),
        "p95": _percentile(ordered, 0.95),
        "max": round(ordered[-1], 6),
    }


def _percentile(ordered: Sequence[float], q: float) -> float:
    """最近名次法（nearest-rank）。**不內插**：門檻要拿一個真的出現過的分數當依據，
    內插出來的 0.4137 在這份資料上沒有任何一段拿過。"""
    index = max(0, math.ceil(q * len(ordered)) - 1)
    return round(ordered[index], 6)


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

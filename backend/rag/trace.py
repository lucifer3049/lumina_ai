"""一次檢索的單據 —— `rag_trace`（06 §7、12 §1.1 Correlation，2B-5）。

06 §7 一句話定義了它：

    每次查詢產生 `rag_trace`（request_id 關聯）：各階段耗時、候選數、rerank 分數
    分布、壓縮率、最終 token 分配、citation 驗證結果。**除錯與評測共用同一 trace**。

**為什麼要有這個型別，而不是繼續各階段各記一行 log。** 這些數字目前散在
`retrieval_completed`、`fts_retrieval_completed`、`rerank_applied`、
`rag_context_selected` 四個事件裡，而它們**拼不起來**：沒有共同的關聯鍵、逐路的原始
分數在 RRF 融合那一刻就被覆蓋掉、citation 的驗證結果落在另一個服務的另一個事件上。
於是「這一題為什麼答錯」要靠人在 log 裡對時間戳——而那件事沒有人會做第二次。

三個設計決定：

1. **一次查詢一筆，不是一階段一筆。** 分母才對得起來：「這個月有多少 % 的查詢降級
   了」在多筆的形狀下會憑空變成兩倍，而每一筆都長得像真的。
2. **只記 id 與分數，不記 chunk 內文。** trace 會被寫進 log 並長期保存（12 §1.1 →
   Loki），而 chunk 內文是租戶的文件——人事規章、合約、病歷。把它寫進 log 等於在
   另一個保存週期、另一套權限之下再存一份客戶資料，而那不會有任何徵兆（鐵則 9 的
   同一條線，只是這次外流的是客戶資料而不是 secrets）。
3. **這一層不碰 ORM、也不認識上層**（鐵則 2）。填值的是
   `services/rag/retrieval.py` 與 `services/conversation/chat.py`，它們各自知道自己
   那一段發生了什麼；這裡只定義形狀與「怎麼算」。
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

from config.logging import get_logger
from rag.retrievers.vector import RetrievedChunk

logger = get_logger(__name__)

__all__ = [
    "MAX_RECORDED_CANDIDATES",
    "TRACE_EVENT",
    "CitationTrace",
    "ContextTrace",
    "RagTrace",
    "RerankTrace",
    "RouteTrace",
    "TraceBuilder",
    "emit",
]

TRACE_EVENT = "rag_trace"

# 一筆 trace 是**一行 JSON**，而它跑在熱路徑上。逐路把 40 段全部記下來的話，尖峰時
# 每次查詢都在往 Loki 送幾 KB——那是 11 §4 實實在在的成本。
#
# **刻意不是可調參數**（15 §4.1 的例外條款，同 `MAX_TOP_K`）：它保護的是日誌管線，
# 不是「由使用者決定」的產品行為。前 20 名足以回答「正解排第幾、分數多少」——那正是
# 這份資料唯一被拿來用的問題。
MAX_RECORDED_CANDIDATES = 20


@dataclass(frozen=True, slots=True)
class RouteTrace:
    """一條檢索路（向量／FTS）在**融合之前**的樣子。

    `top_scores` 是這一路自己尺度上的分數（餘弦相似度、pgroonga 分數），**這是它
    存在的全部理由**：`fuse_candidates` 會把 `score` 換成名次倒數和（第一名
    ≈ 0.016），原本的分數在那之後永遠查不回來。`rag/pipeline.py` 的 docstring 自
    2B-2 起就把這筆帳記在 2B-5 名下。
    """

    name: str
    kb_id: str
    candidate_count: int
    elapsed_ms: float
    top_scores: tuple[float, ...] = ()
    top_chunk_ids: tuple[str, ...] = ()
    # **棄權不是降級**（2B-2b）：「這句話裡沒有字面比對幫得上忙的東西」是正確答案，
    # 而降級指的是「該做卻做不到」。混在一起的話 `degraded` 會被純中文問句灌爆，
    # 真正的故障就淹在裡面。
    abstained: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kb_id": self.kb_id,
            "candidate_count": self.candidate_count,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "top_scores": [round(score, 6) for score in self.top_scores],
            "top_chunk_ids": list(self.top_chunk_ids),
            "abstained": self.abstained,
        }


@dataclass(frozen=True, slots=True)
class RerankTrace:
    """cross-encoder 那一段。`scores` 是 2B-4 結案缺口①要的那份分布。

    **降級時 `scores` 必須是空的。** 那時手上是 RRF 的融合分數，把它記進來的話，
    之後拿來裁決絕對門檻的分布裡會混進一堆 0.016——而那看起來只是「分數偏低」，
    不是「這幾筆根本不是 cross-encoder 給的」。
    """

    applied: bool
    candidate_count: int
    kept_count: int
    threshold: float
    elapsed_ms: float
    scores: tuple[float, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "candidate_count": self.candidate_count,
            "kept_count": self.kept_count,
            "threshold": self.threshold,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "scores": [round(score, 6) for score in self.scores],
        }


@dataclass(frozen=True, slots=True)
class ContextTrace:
    """06 §7 的「壓縮率、最終 token 分配」。

    被裁掉的那幾段是**看不見的損失**：回答只是「答得不夠完整」，而沒有任何地方
    指向裁切。`compression_ratio` 讓它變成一個看得見的數字。
    """

    chunk_count: int
    candidate_count: int
    tokens: int
    candidate_tokens: int
    token_budget: int

    @property
    def compression_ratio(self) -> float:
        """進 context 的 token ÷ 候選的 token。1.0 = 一段都沒被裁掉。"""
        if self.candidate_tokens <= 0:
            return 1.0
        return self.tokens / self.candidate_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_count": self.chunk_count,
            "candidate_count": self.candidate_count,
            "tokens": self.tokens,
            "candidate_tokens": self.candidate_tokens,
            "token_budget": self.token_budget,
            "compression_ratio": round(self.compression_ratio, 4),
        }


@dataclass(frozen=True, slots=True)
class CitationTrace:
    """06 §3.3 的幻覺指標。與 `messages.usage.rag` 記的是同一件事（1D-5）。

    兩份數字漂掉時，3B 的評測報表與除錯會給出互相矛盾的答案，而沒有人知道該信哪
    一份——`test_rag_trace_correlation.py` 因此把兩邊釘在一起。
    """

    citations: int
    dropped: int

    def as_dict(self) -> dict[str, int]:
        return {"citations": self.citations, "dropped": self.dropped}


@dataclass(frozen=True, slots=True)
class RagTrace:
    """一次檢索的完整單據。**檢索那一半在 `services/rag/`，引用那一半在收尾時補上。**"""

    mode: str
    elapsed_ms: float
    stages: Mapping[str, float] = field(default_factory=dict)
    routes: tuple[RouteTrace, ...] = ()
    fused_count: int = 0
    rerank: RerankTrace | None = None
    context: ContextTrace | None = None
    citations: CitationTrace | None = None
    degraded: tuple[str, ...] = ()

    def with_citations(self, *, citations: int, dropped: int) -> RagTrace:
        """收尾時補上引用的驗證結果（`ChatService`）。

        **回一份新的而不是就地改**：這個物件會跨 await 傳遞，而就地改代表兩個併發
        的回合可能踩到同一份——那種錯誤的症狀是「偶爾有一筆 trace 的數字對不上」。
        """
        return replace(self, citations=CitationTrace(citations=citations, dropped=dropped))

    def as_dict(self) -> dict[str, Any]:
        """一行 JSON 的內容。**不含任何 chunk 內文**（見模組 docstring 第 2 點）。"""
        return {
            "mode": self.mode,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "stages": {name: round(value, 3) for name, value in self.stages.items()},
            "routes": [route.as_dict() for route in self.routes],
            "fused_count": self.fused_count,
            "rerank": self.rerank.as_dict() if self.rerank else None,
            "context": self.context.as_dict() if self.context else None,
            "citations": self.citations.as_dict() if self.citations else None,
            # 正常路徑是**空清單而不是省略欄位**：省略的話，「這一趟沒有降級」與
            # 「這個版本還沒有這個欄位」在報表上分不出來。
            "degraded": list(self.degraded),
        }


class TraceBuilder:
    """邊跑邊記。**可變的，而且一次檢索一個**——共用一個 builder 會讓兩個併發的
    查詢互相污染，而症狀是「候選數偶爾是別人的」。

    `request_id` 不在這裡：它由 structlog 的 contextvars 帶（12 §1.1），逐層傳參數
    的話，總有一條路徑會忘了傳，而那一筆就此與其他事件失去關聯。
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self._started = time.perf_counter()
        self._stages: dict[str, float] = {}
        self._routes: list[RouteTrace] = []
        self._fused_count = 0
        self._rerank: RerankTrace | None = None
        self._context: ContextTrace | None = None

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """量一段的耗時並**累加**（同一階段可能跑很多次：多 KB、多路）。

        各段必須**互不重疊**，否則 `sum(stages) > elapsed_ms`——而那種帳看起來只是
        「數字怪怪的」，不會有人發現它在數學上不可能。
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            self._stages[name] = self._stages.get(name, 0.0) + _ms_since(started)

    def route(
        self,
        name: str,
        *,
        kb_id: str,
        chunks: Sequence[RetrievedChunk],
        elapsed_ms: float,
        abstained: bool = False,
    ) -> None:
        kept = list(chunks)[:MAX_RECORDED_CANDIDATES]
        self._routes.append(
            RouteTrace(
                name=name,
                kb_id=kb_id,
                candidate_count=len(chunks),
                elapsed_ms=elapsed_ms,
                top_scores=tuple(chunk.score for chunk in kept),
                top_chunk_ids=tuple(str(chunk.chunk_id) for chunk in kept),
                abstained=abstained,
            )
        )

    def fused(self, count: int) -> None:
        self._fused_count = count

    def reranked(
        self,
        *,
        applied: bool,
        candidates: int,
        kept: Sequence[RetrievedChunk],
        threshold: float,
        elapsed_ms: float,
    ) -> None:
        self._rerank = RerankTrace(
            applied=applied,
            candidate_count=candidates,
            kept_count=len(kept) if applied else candidates,
            threshold=threshold,
            elapsed_ms=elapsed_ms,
            # 降級時不記分數（見 `RerankTrace` 的 docstring）。
            scores=tuple(chunk.score for chunk in kept[:MAX_RECORDED_CANDIDATES])
            if applied
            else (),
        )

    def selected(
        self,
        *,
        chunks: Sequence[RetrievedChunk],
        candidates: Sequence[RetrievedChunk],
        token_budget: int,
        count_tokens: Any,
    ) -> None:
        """進 context 的那幾段。`count_tokens` 由呼叫端傳入——與 chunker 是**同一個
        函式**，兩邊估法不同時壓縮率會對不起來。"""
        self._context = ContextTrace(
            chunk_count=len(chunks),
            candidate_count=len(candidates),
            tokens=sum(count_tokens(chunk.content) for chunk in chunks),
            candidate_tokens=sum(count_tokens(chunk.content) for chunk in candidates),
            token_budget=token_budget,
        )

    def build(self, *, degraded: Sequence[str] = ()) -> RagTrace:
        return RagTrace(
            mode=self.mode,
            elapsed_ms=_ms_since(self._started),
            stages=dict(self._stages),
            routes=tuple(self._routes),
            fused_count=self._fused_count,
            rerank=self._rerank,
            context=self._context,
            degraded=tuple(degraded),
        )


def emit(trace: RagTrace) -> None:
    """把單據寫成一行 JSON（12 §1.1）。

    `request_id` / `tenant_id` 由 structlog 的 contextvars 自動帶上——**包含背景生成
    那條路**：`asyncio.create_task` 會複製當下的 context，所以請求早已結束、
    middleware 的 finally 也早已跑過，子 task 仍看得到那次請求的 id。這件事由
    `test_rag_trace_correlation.py` 釘住，因為改成 threadpool 或 Celery 就會斷，而
    斷掉之後 trace 照常寫、只是關聯不上了。
    """
    logger.info(TRACE_EVENT, **trace.as_dict())


def _ms_since(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0

"""RetrievalService —— 讀路徑的第一段（06 §3、13 §3 工作包 1C-4／1D-5）。

**編排四件事**：把問題變成向量（經 Gateway，鐵則 5）、去 DB 找最相近的 chunk、
跨 KB 合併、裁到 context 放得下。演算法在 `rag/`、SQL 在 `repositories/`，這一層只
負責串起來——與 `IngestionService` 對 `etl/` 的關係完全相同。

**查詢與文件必須用同一個模型**（06 §2.2）：向量只有在同一個模型的空間裡才可比較。
模型與版本因此都從 KB 讀，而不是從設定讀——KB 是重嵌入時唯一會被原子切換的地方，
兩邊各自取值的話，切換的那一刻查詢會拿新模型的向量去比對舊模型的資料，而距離照樣
算得出來，只是排序完全沒有意義。

**參數一律經 `services/rag/params.py`**（15 §4.1）。1D-5 之前 `top_k=40` 同時寫在本檔
的常數與 `/rag/query` 的簽章上，兩份會漂——而漂掉的症狀是「除錯用的 API 查得到、
實際問答查不到」，那時沒有人會想到去比對兩個預設值。

1C-4 只有純向量（13 的「純向量檢索先行」）。FTS 與 RRF 融合排 Phase 2、rerank 排 2B，
兩者都接在 `merge_candidates` 前後，形狀不必動。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace

from ai.gateway import AIGateway, build_gateway
from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from core.exceptions import NotFoundError
from core.tenant import tenant_context
from core.uow import unit_of_work
from rag.pipeline import fuse_candidates, gate_by_absolute_score, gate_by_score, select_context
from rag.retrievers.keyword import build_fts_query
from rag.retrievers.vector import RetrievedChunk, normalise_query, to_retrieved
from repositories.knowledge import ChunkRepository, EmbeddingRepository, KnowledgeBaseRepository
from services.knowledge.embedding import model_for
from services.rag.params import MAX_TOP_K, RagParams, resolve_rag_params

logger = get_logger(__name__)

__all__ = ["MAX_TOP_K", "RetrievalOutcome", "RetrievalService", "default_top_k"]

# 11 §2：`ef_search=80` 起步（HNSW 的「找多仔細」旋鈕，預設值 40 比這低）。
# 不設的話召回會比評測時差一截，而那個差距不會出現在任何錯誤訊息裡。
#
# **刻意不進可調參數區**（15 §4.1）：它是索引行為的旋鈕，不是產品決策——調它要照
# 11 §2 的量測方法做，不是在後台拉一個滑桿。
EF_SEARCH = 80


def default_top_k() -> int:
    """`/rag/query` 的預設值——**與問答走同一個來源**（15 §4.1）。

    兩邊各寫一個數字的話，「除錯 API 查得到、實際問答查不到」會變成一種可能，而那時
    沒有人會想到去比對兩個預設值。
    """
    return resolve_rag_params(None).top_k


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    """一次檢索的結果 + **哪幾個增強步驟被跳過了**。

    降級要說得出來（06 §1 的「降級優先於失敗」只講了一半）：rerank 掛掉時答案仍然
    出得來，差別只在排序——沒有標記的話，「品質變差」在任何地方都查不到，而評測分數
    掉一截時沒有人會想到是 rerank 靜靜地停了三天。標記一路走到 `usage.rag.degraded`
    （1D-5 的 `usage.rag` 子物件）。
    """

    chunks: list[RetrievedChunk]
    degraded: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _EmbeddedQuery:
    """查詢向量 + provider 實際用的模型名。後者是搜尋的過濾條件之一（見 `_search`）。"""

    vector: list[float]
    model: str
    prompt_tokens: int


@dataclass(frozen=True, slots=True)
class _KnowledgeBaseSnapshot:
    """檢索要用到的 KB 設定，**已經離開 ORM**。

    在交易內就把欄位取出來：離開 `unit_of_work` 之後再碰 model 屬性會觸發新的查詢，
    而那時已經沒有租戶 context 了（RLS 讀不到東西，症狀是 `DoesNotExist`）。
    """

    id: uuid.UUID
    embedding_model: str
    embedding_version: int
    config: dict[str, object]


class RetrievalService:
    def __init__(
        self,
        *,
        gateway: AIGateway | None = None,
        knowledge_bases: KnowledgeBaseRepository | None = None,
        embeddings: EmbeddingRepository | None = None,
        chunks: ChunkRepository | None = None,
    ) -> None:
        # Gateway 惰性建立，理由同 EmbeddingService：`build_gateway()` 會解析 provider
        # 名稱，而未實作的 provider 直接 raise——建構 service 本身不該因此失敗。
        self._gateway = gateway
        self._knowledge_bases = knowledge_bases or KnowledgeBaseRepository()
        self._embeddings = embeddings or EmbeddingRepository()
        self._chunks = chunks or ChunkRepository()

    @property
    def gateway(self) -> AIGateway:
        if self._gateway is None:
            self._gateway = build_gateway()
        return self._gateway

    def query(
        self,
        tenant_id: uuid.UUID,
        *,
        kb_id: uuid.UUID,
        query: str,
        top_k: int | None = None,
        mode: str | None = None,
    ) -> RetrievalOutcome:
        """在一個 KB 內檢索最相關的 chunk（`/rag/query` 的路徑）。

        `mode` 覆寫這個 KB 生效中的檢索模式，**只給離線評測用**
        （`scripts/eval_retrieval.py` 要能在同一份資料上量出 `vector` 與 `hybrid` 的
        差距）。正常路徑一律傳 `None`，讓 15 §4.1 的那條解析說了算。

        KB 不存在（或屬於別的租戶）時 raise `NotFoundError`——**不是回空清單**。
        空清單的意思是「這個 KB 存在但沒有相關內容」，兩者對呼叫端的處置完全不同，
        而 09 §2.3 要求跨租戶的資源一律 404（403 等於承認那個 id 存在）。
        """
        text = normalise_query(query)
        if not text:
            raise ValueError("查詢不得為空")

        kb = self._find_kb(tenant_id, kb_id)
        if kb is None:
            raise NotFoundError("知識庫不存在")

        params = resolve_rag_params(kb.config)
        limit = params.top_k if top_k is None else top_k
        if not 1 <= limit <= MAX_TOP_K:
            raise ValueError(f"top_k 必須介於 1 與 {MAX_TOP_K} 之間")

        groups, degraded = self._retrieve(tenant_id, kb, text, params, top_k=limit, mode=mode)
        # **`/rag/query` 與問答看到同一組候選**：這個端點存在的理由就是「看檢索準不
        # 準」，兩邊各自融合一次的話，1D-5 那個「除錯 API 查得到、實際問答查不到」的
        # 情境會換個位置重演。
        fused = fuse_candidates(groups, k=params.rrf_k, limit=params.hybrid_candidates)
        return self._rerank(text, fused, params, mode=mode, degraded=degraded)

    def retrieve_for_chat(
        self,
        tenant_id: uuid.UUID,
        *,
        kb_ids: Sequence[uuid.UUID],
        query: str,
        mode: str | None = None,
    ) -> RetrievalOutcome:
        """一場對話掛著的所有 KB → 真正進 context 的那幾段（1D-5）。

        **找不到的 KB 跳過，不讓整輪失敗。** 對話是長命的，而 KB 可以在對話中途被刪掉
        （`kb_ids` 是建立當下存下來的一串 id，沒有任何東西保證它們還在、或屬於這個
        租戶）。整輪失敗的話，那場對話從此每一次發言都失敗，直到使用者放棄它——而他
        看到的只是「一直出錯」。跳過之後模型會依模板規則 3 誠實說自己不知道，而
        warning 留在 log 裡供追查。

        參數取**第一個解析得到的 KB** 的設定：多 KB 各有各的 config 時，context 上限
        這種「整段對話共用」的東西沒有合併語意可言，而取第一個至少是可預期的。
        """
        text = normalise_query(query)
        if not text or not kb_ids:
            # 沒掛 KB 就是純閒聊路徑（06 §9），連一次 embedding 的錢都不該付。
            return RetrievalOutcome(chunks=[])

        available = [kb for kb in (self._find_kb(tenant_id, kb_id) for kb_id in kb_ids) if kb]
        for kb_id in set(kb_ids) - {kb.id for kb in available}:
            logger.warning("rag_kb_unavailable", kb_id=str(kb_id))
        if not available:
            return RetrievalOutcome(chunks=[])

        params = resolve_rag_params(available[0].config)
        # **同一個模型只算一次向量。** 查詢向量與 KB 無關，只與模型有關；每個 KB 各
        # 算一次等於同一段文字付 N 次錢，而結果完全相同。不能無條件只算一次的原因是
        # 各 KB 的 `embedding_model` 可以不同（06 §2.2：向量只在同一個模型的空間裡
        # 可比較），因此快取的鍵是模型而不是「這次查詢」。
        cache: dict[str, _EmbeddedQuery] = {}
        groups: list[list[RetrievedChunk]] = []
        degraded: tuple[str, ...] = ()
        for kb in available:
            kb_groups, kb_degraded = self._retrieve(
                tenant_id, kb, text, params, top_k=params.top_k, mode=mode, cache=cache
            )
            groups.extend(kb_groups)
            # 多 KB 時任何一個 KB 的 FTS 出事都算降級：使用者感受到的是「答案變差」，
            # 而那與「只有第二個知識庫的字面檢索掛了」在畫面上沒有分別。
            degraded = tuple(dict.fromkeys([*degraded, *kb_degraded]))

        fused = fuse_candidates(groups, k=params.rrf_k, limit=params.hybrid_candidates)
        reranked = self._rerank(text, fused, params, mode=mode, degraded=degraded)
        selected = select_context(
            reranked.chunks,
            max_chunks=params.context_chunks,
            token_budget=params.context_token_budget,
        )
        logger.info(
            "rag_context_selected",
            kb_count=len(available),
            group_count=len(groups),
            candidate_count=sum(len(group) for group in groups),
            context_count=len(selected),
            degraded=list(reranked.degraded),
        )
        return RetrievalOutcome(chunks=selected, degraded=reranked.degraded)

    def params_for(self, tenant_id: uuid.UUID, kb_ids: Sequence[uuid.UUID]) -> RagParams:
        """這場對話的檢索參數。`ChatService` 用它決定查詢要往前帶幾個問題。

        沒有可用的 KB 時回系統預設——那時也不會有檢索，取值只是為了讓呼叫端不必
        處理 `None`。
        """
        for kb_id in kb_ids:
            kb = self._find_kb(tenant_id, kb_id)
            if kb is not None:
                return resolve_rag_params(kb.config)
        return resolve_rag_params(None)

    # ── 內部 ────────────────────────────────────────────────────

    def _retrieve(
        self,
        tenant_id: uuid.UUID,
        kb: _KnowledgeBaseSnapshot,
        text: str,
        params: RagParams,
        *,
        top_k: int,
        mode: str | None,
        cache: dict[str, _EmbeddedQuery] | None = None,
    ) -> tuple[list[list[RetrievedChunk]], tuple[str, ...]]:
        """一個 KB → 各路的候選清單（融合前）。

        **門檻在這裡套、逐路各自套**（`rag/pipeline.py` 的 `gate_by_score`）：融合之後
        分數換成名次倒數和，相對門檻在那個尺度上不是砍不掉東西就是把大半砍光。

        回傳的是 list of list 而不是攤平的一份：RRF 吃的是「每一路各自的名次」，攤平
        之後那個資訊就沒有了。
        """
        effective = mode or params.retrieval_mode
        groups = [
            gate_by_score(
                self._search(tenant_id, kb, text, top_k=top_k, cache=cache),
                min_score_ratio=params.min_score_ratio,
            )
        ]
        degraded: tuple[str, ...] = ()
        if effective.startswith("hybrid"):
            keyword, failed = self._search_fts(tenant_id, kb, text, top_k=params.fts_top_k)
            if failed:
                degraded = ("fts",)
            if keyword:
                groups.append(gate_by_score(keyword, min_score_ratio=params.min_score_ratio))
        return groups, degraded

    def _rerank(
        self,
        text: str,
        candidates: list[RetrievedChunk],
        params: RagParams,
        *,
        mode: str | None,
        degraded: tuple[str, ...],
    ) -> RetrievalOutcome:
        """融合後的候選 → cross-encoder 重排 → 絕對門檻（06 §3.1 的 Rerank 階段，2B-3）。

        **失敗一律降級成「維持融合順序」，不往外拋**（06 §1）：rerank 是可跳過的增強，
        而它是讀路徑上最脆弱的一環（06 §6）——外部服務、GPU、模型載入，任何一項出事都
        不該讓使用者問不了問題。

        **絕對門檻只在 rerank 真的跑完時才套用。** 這不是保險而是正確性：門檻 0.3 是
        cross-encoder 的尺度，而降級之後手上是 RRF 的融合分數（第一名 1/61 ≈ 0.016），
        套上去會把候選**全部**砍光——使用者看到的是「這個知識庫突然什麼都答不出來」，
        而 log 裡只有一行降級 warning。1D-5 當初拒絕在 Phase 1 啟用它，就是同一個理由。

        `top_n` 直接用 `context_chunks`（06 §3.1 的 top_n 6~8 **就是**「進 context 幾段」）
        ——兩個數字分開會漂，而漂掉的症狀是「rerank 排了 8 段、context 只放得下 6 段」：
        花了 cross-encoder 的錢卻把它排最好的那兩段丟掉。
        """
        effective = mode or params.retrieval_mode
        if not effective.endswith("+rerank") or not candidates:
            return RetrievalOutcome(chunks=candidates, degraded=degraded)

        settings = get_app_settings()
        try:
            result = self.gateway.rerank(
                text,
                [chunk.content for chunk in candidates],
                model=settings.ai_rerank_model,
                top_n=params.context_chunks,
                timeout_seconds=settings.ai_rerank_timeout_seconds,
            )
        # 同 FTS：什麼錯都接得住是降級的前提（provider、連線、逾時都在這裡收斂）。
        except Exception as exc:
            logger.warning("rag_rerank_degraded", error=type(exc).__name__, detail=str(exc))
            return RetrievalOutcome(
                chunks=candidates, degraded=tuple(dict.fromkeys([*degraded, "rerank"]))
            )

        reordered = [
            replace(candidates[doc.index], score=doc.score)
            for doc in result.documents
            # provider 回一個超出範圍的索引是它的 bug，但代價會落在我們身上
            # （IndexError → 整輪失敗）。忽略掉並讓其餘的照常。
            if 0 <= doc.index < len(candidates)
        ]
        kept = gate_by_absolute_score(reordered, threshold=params.rerank_threshold)
        logger.info(
            "rerank_applied",
            candidate_count=len(candidates),
            kept_count=len(kept),
            threshold=params.rerank_threshold,
        )
        return RetrievalOutcome(chunks=kept, degraded=degraded)

    def _search_fts(
        self, tenant_id: uuid.UUID, kb: _KnowledgeBaseSnapshot, text: str, *, top_k: int
    ) -> tuple[list[RetrievedChunk], bool]:
        """字面比對那一路。**失敗一律降級成「這一路沒有候選」，不往外拋。**

        06 §1 的「降級優先於失敗」：FTS 是增強而不是必要。索引壞掉、pgroonga 沒裝、
        planner 選了一個 `&@*` 用不了的計畫——這些的正確處置都是退回純向量，而不是讓
        使用者從此問不了問題。整輪失敗的代價是「這個知識庫壞了」，降級的代價只是
        「稀有詞暫時找不到」。

        **但一定要留下痕跡**：安靜的降級會讓「檢索品質變差」查不到原因，而那時看得到
        的只有評測分數掉了一截。
        """
        query = build_fts_query(text)
        if not query:
            # **沒有識別符就棄權**（2B-2b）：純中文的概念問句與純小寫的英文問句裡沒有
            # 字面比對幫得上忙的東西，投一張模糊票的代價是把向量找對的答案擠下去
            # ——2B-2 的評測實測到這件事。理由見 `rag/retrievers/keyword.py`。
            # 棄權**不是降級**：它是「這句話裡沒有字面比對幫得上忙的東西」的正確
            # 答案，而降級指的是「該做卻做不到」。混在一起的話 `usage.rag.degraded`
            # 會被純中文問句灌爆，而真正的故障就淹在裡面了。
            logger.info("fts_abstained", kb_id=str(kb.id))
            return [], False

        try:
            with tenant_context(tenant_id), unit_of_work():
                hits = self._chunks.search_fts(query, kb_id=kb.id, top_k=top_k)
        # 什麼錯都接得住是降級的前提：這裡的例外可能來自 DB（索引不存在）、PGroonga
        # （計畫選錯）、甚至連線層。分類它們只會讓下一種沒想到的錯誤變成 500。
        except Exception as exc:
            logger.warning(
                "rag_fts_degraded", kb_id=str(kb.id), error=type(exc).__name__, detail=str(exc)
            )
            return [], True

        results = to_retrieved(hits)
        logger.info(
            "fts_retrieval_completed", kb_id=str(kb.id), top_k=top_k, hit_count=len(results)
        )
        return results, False

    def _search(
        self,
        tenant_id: uuid.UUID,
        kb: _KnowledgeBaseSnapshot,
        text: str,
        *,
        top_k: int,
        cache: dict[str, _EmbeddedQuery] | None = None,
    ) -> list[RetrievedChunk]:
        embedded = self._embed(text, model_for(kb.embedding_model), cache)

        with tenant_context(tenant_id), unit_of_work():
            hits = self._embeddings.search(
                embedded.vector,
                kb_id=kb.id,
                # provider 回報的 model：寫入時記的就是這個（1C-3），別名解析之後
                # 請求值與實際值可能不同，而唯一鍵記的是實際值。
                model=embedded.model,
                embedding_version=kb.embedding_version,
                top_k=top_k,
                ef_search=EF_SEARCH,
            )

        results = to_retrieved(hits)
        logger.info(
            "retrieval_completed",
            kb_id=str(kb.id),
            model=embedded.model,
            top_k=top_k,
            hit_count=len(results),
            prompt_tokens=embedded.prompt_tokens,
        )
        return results

    def _embed(
        self, text: str, model: str, cache: dict[str, _EmbeddedQuery] | None
    ) -> _EmbeddedQuery:
        """查詢向量。同一次檢索內、同一個模型只算一次（見 `retrieve_for_chat`）。"""
        if cache is not None and model in cache:
            return cache[model]
        # 查詢向量與文件向量走同一個 Gateway、同一個模型（見模組 docstring）。
        result = self.gateway.embed([text], model=model)
        embedded = _EmbeddedQuery(
            vector=list(result.vectors[0]),
            model=result.model,
            prompt_tokens=result.usage.total_tokens,
        )
        if cache is not None:
            cache[model] = embedded
        return embedded

    def _find_kb(self, tenant_id: uuid.UUID, kb_id: uuid.UUID) -> _KnowledgeBaseSnapshot | None:
        with tenant_context(tenant_id), unit_of_work():
            kb = self._knowledge_bases.get_by_id(kb_id)
            if kb is None:
                return None
            return _KnowledgeBaseSnapshot(
                id=uuid.UUID(str(kb.id)),
                embedding_model=str(kb.embedding_model),
                embedding_version=int(kb.embedding_version),
                config=dict(kb.config or {}),
            )

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
from dataclasses import dataclass

from ai.gateway import AIGateway, build_gateway
from config.logging import get_logger
from core.exceptions import NotFoundError
from core.tenant import tenant_context
from core.uow import unit_of_work
from rag.pipeline import merge_candidates, select_context
from rag.retrievers.vector import RetrievedChunk, normalise_query, to_retrieved
from repositories.knowledge import EmbeddingRepository, KnowledgeBaseRepository
from services.knowledge.embedding import model_for
from services.rag.params import MAX_TOP_K, RagParams, resolve_rag_params

logger = get_logger(__name__)

__all__ = ["MAX_TOP_K", "RetrievalService", "default_top_k"]

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
    ) -> None:
        # Gateway 惰性建立，理由同 EmbeddingService：`build_gateway()` 會解析 provider
        # 名稱，而未實作的 provider 直接 raise——建構 service 本身不該因此失敗。
        self._gateway = gateway
        self._knowledge_bases = knowledge_bases or KnowledgeBaseRepository()
        self._embeddings = embeddings or EmbeddingRepository()

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
    ) -> list[RetrievedChunk]:
        """在一個 KB 內檢索最相關的 chunk（`/rag/query` 的路徑）。

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

        limit = resolve_rag_params(kb.config).top_k if top_k is None else top_k
        if not 1 <= limit <= MAX_TOP_K:
            raise ValueError(f"top_k 必須介於 1 與 {MAX_TOP_K} 之間")

        return self._search(tenant_id, kb, text, top_k=limit)

    def retrieve_for_chat(
        self, tenant_id: uuid.UUID, *, kb_ids: Sequence[uuid.UUID], query: str
    ) -> list[RetrievedChunk]:
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
            return []

        available = [kb for kb in (self._find_kb(tenant_id, kb_id) for kb_id in kb_ids) if kb]
        for kb_id in set(kb_ids) - {kb.id for kb in available}:
            logger.warning("rag_kb_unavailable", kb_id=str(kb_id))
        if not available:
            return []

        params = resolve_rag_params(available[0].config)
        # **同一個模型只算一次向量。** 查詢向量與 KB 無關，只與模型有關；每個 KB 各
        # 算一次等於同一段文字付 N 次錢，而結果完全相同。不能無條件只算一次的原因是
        # 各 KB 的 `embedding_model` 可以不同（06 §2.2：向量只在同一個模型的空間裡
        # 可比較），因此快取的鍵是模型而不是「這次查詢」。
        cache: dict[str, _EmbeddedQuery] = {}
        groups = [
            self._search(tenant_id, kb, text, top_k=params.top_k, cache=cache) for kb in available
        ]
        selected = select_context(
            merge_candidates(groups),
            max_chunks=params.context_chunks,
            token_budget=params.context_token_budget,
            min_score_ratio=params.min_score_ratio,
        )
        logger.info(
            "rag_context_selected",
            kb_count=len(available),
            candidate_count=sum(len(group) for group in groups),
            context_count=len(selected),
        )
        return selected

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

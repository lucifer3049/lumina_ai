"""RetrievalService —— 讀路徑的第一段（06 §3、13 §3 工作包 1C-4）。

**編排三件事**：把問題變成向量（經 Gateway，鐵則 5）、去 DB 找最相近的 chunk、把結果
整理成下游要的形狀。演算法在 `rag/`、SQL 在 `repositories/`，這一層只負責串起來——
與 `IngestionService` 對 `etl/` 的關係完全相同。

**查詢與文件必須用同一個模型**（06 §2.2）：向量只有在同一個模型的空間裡才可比較。
模型與版本因此都從 KB 讀，而不是從設定讀——KB 是重嵌入時唯一會被原子切換的地方，
兩邊各自取值的話，切換的那一刻查詢會拿新模型的向量去比對舊模型的資料，而距離照樣
算得出來，只是排序完全沒有意義。

1C-4 只有純向量（13 的「純向量檢索先行」）。FTS 與 RRF 融合排 Phase 2、rerank 排 2B，
兩者都接在 `_search` 之後，形狀不必動。
"""

from __future__ import annotations

import uuid

from ai.gateway import AIGateway, build_gateway
from config.logging import get_logger
from core.exceptions import NotFoundError
from core.tenant import tenant_context
from core.uow import unit_of_work
from rag.retrievers.vector import RetrievedChunk, normalise_query, to_retrieved
from repositories.knowledge import EmbeddingRepository, KnowledgeBaseRepository
from services.knowledge.embedding import model_for

logger = get_logger(__name__)

# 06 §3.1：vector search top_k=40。
#
# 定在 service 而不是散在呼叫端：1D 與 `/rag/query` 必須拿到同一組候選，否則「除錯用
# 的 API 查得到、實際問答查不到」會變成一種可能，而那時沒有人會想到去比對兩個預設值。
DEFAULT_TOP_K = 40

# 上限。`top_k` 直接進 SQL 的 LIMIT，而呼叫端是外部整合方——沒有上限的話，一個
# `top_k=1000000` 的請求會讓 pgvector 把整個 KB 的向量掃出來排序。那不會失敗，
# 只會讓那台 DB 在那幾秒內對**所有租戶**都很慢。
MAX_TOP_K = 200

# 11 §2：`ef_search=80` 起步（HNSW 的「找多仔細」旋鈕，預設值 40 比這低）。
# 不設的話召回會比評測時差一截，而那個差距不會出現在任何錯誤訊息裡。
EF_SEARCH = 80


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
        top_k: int = DEFAULT_TOP_K,
    ) -> list[RetrievedChunk]:
        """在一個 KB 內檢索最相關的 chunk。

        KB 不存在（或屬於別的租戶）時 raise `NotFoundError`——**不是回空清單**。
        空清單的意思是「這個 KB 存在但沒有相關內容」，兩者對呼叫端的處置完全不同，
        而 09 §2.3 要求跨租戶的資源一律 404（403 等於承認那個 id 存在）。
        """
        text = normalise_query(query)
        if not text:
            raise ValueError("查詢不得為空")
        if not 1 <= top_k <= MAX_TOP_K:
            raise ValueError(f"top_k 必須介於 1 與 {MAX_TOP_K} 之間")

        model, embedding_version = self._embedding_config(tenant_id, kb_id)
        # 查詢向量與文件向量走同一個 Gateway、同一個模型（見模組 docstring）。
        embedded = self.gateway.embed([text], model=model)

        with tenant_context(tenant_id), unit_of_work():
            hits = self._embeddings.search(
                embedded.vectors[0],
                kb_id=kb_id,
                # provider 回報的 model：寫入時記的就是這個（1C-3），別名解析之後
                # 請求值與實際值可能不同，而唯一鍵記的是實際值。
                model=embedded.model,
                embedding_version=embedding_version,
                top_k=top_k,
                ef_search=EF_SEARCH,
            )

        results = to_retrieved(hits)
        logger.info(
            "retrieval_completed",
            kb_id=str(kb_id),
            model=embedded.model,
            top_k=top_k,
            hit_count=len(results),
            prompt_tokens=embedded.usage.total_tokens,
        )
        return results

    def _embedding_config(self, tenant_id: uuid.UUID, kb_id: uuid.UUID) -> tuple[str, int]:
        with tenant_context(tenant_id), unit_of_work():
            kb = self._knowledge_bases.get_by_id(kb_id)
            if kb is None:
                raise NotFoundError("知識庫不存在")
            return model_for(kb.embedding_model), int(kb.embedding_version)

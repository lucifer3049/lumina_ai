"""驗收：**刪掉的文件不能再被檢索到**（05 §5.4、06 §3.1；二次架構審計 H1）。

刪除路徑原本只軟刪 `knowledge_document` 那一列。可是檢索讀的是 chunk 與 embedding
兩張表，而兩路的過濾條件是租戶／KB／``superseded``／模型版本——**沒有一路認得
``deleted_at``**（軟刪除的可見性規則實作在 `DocumentRepository.get_queryset`，chunk
不繼承它）。於是使用者刪掉文件、API 回 204、列表消失、額度立刻釋放，然後那份文件的
內容與**檔名**繼續出現在後續問答的 context 與 `citations` 裡，點進去的引用是 404。

這件事錯了不會有任何錯誤訊息：回答看起來完全正常，只是它引用了一份不該存在的文件。
放 integration 而不是 unit，理由同 `test_vector_retrieval.py`：要驗的是 DB 的過濾條件
真的把那些列擋掉了，用假物件驗這一層等於什麼都沒驗。

三條路各驗一次，因為它們**各有各的過濾條件**，改一路不會讓另一路跟著對：
向量（`EmbeddingRepository.search` 的 ORM 條件）、FTS（`FTS_SQL` 的手寫 WHERE）、
以及清理 job（標記完之後那些列要真的被硬刪，否則只是把成本問題往後推）。
"""

from __future__ import annotations

import uuid

import pytest

from ai.gateway import AIGateway
from ai.gateway.providers.mock import MockEmbeddingProvider
from apps.knowledge.models import Chunk
from rag.retrievers.keyword import build_fts_query
from repositories.knowledge import ChunkRepository
from services.knowledge.cleanup import ChunkCleanupService
from services.knowledge.documents import DocumentService
from services.knowledge.embedding import EmbeddingService
from services.rag.retrieval import RetrievalService
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_chunk, make_document, make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)

# 兩份文件，各一段**只出現在自己身上**的稀有識別符——FTS 的 2B-2b 策略是「問句裡有
# 識別符才發言」，純中文概念問句會棄權（見 `rag/retrievers/keyword.py`）。
_SECRET = "存取權杖的簽章演算法採 ES256，API 節點只需要公鑰就能驗簽。"
_KEPT = "檢索候選集走 partial index，名稱是 ix_chunk_tenant_kb_active。"


def _gateway() -> AIGateway:
    return AIGateway(embedding_provider=MockEmbeddingProvider(), retry_backoff_seconds=())


@pytest.fixture
def tenant() -> None:
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a")


def _kb_with_two_documents(*, status: str = "ready") -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """一個 KB、兩份各一段內容的文件；回傳 (kb_id, 待刪文件, 保留文件)。

    **走真的 `EmbeddingService`** 而不是自己塞 Embedding 列：那條寫路徑決定了向量的
    model 與 version，而檢索必須用同一組值去找（理由同 `test_vector_retrieval.py`）。
    """
    with tenant_scope(TENANT_A):
        kb = make_knowledge_base(tenant_id=TENANT_A)
        doomed = make_document(kb=kb, status=status, filename="即將刪除.pdf")
        kept = make_document(kb=kb, status=status, filename="留著的.pdf")
        for document, content in ((doomed, _SECRET), (kept, _KEPT)):
            make_chunk(
                document=document,
                seq=0,
                content=content,
                meta={"page": 1, "heading_path": ["第一章"]},
            )
    for document in (doomed, kept):
        EmbeddingService(gateway=_gateway()).embed_document(TENANT_A, document.id)
    return (
        uuid.UUID(str(kb.id)),
        uuid.UUID(str(doomed.id)),
        uuid.UUID(str(kept.id)),
    )


def _vector_hits(kb_id: uuid.UUID, question: str) -> list[uuid.UUID]:
    outcome = RetrievalService(gateway=_gateway()).query(TENANT_A, kb_id=kb_id, query=question)
    return [chunk.document_id for chunk in outcome.chunks]


def _fts_hits(kb_id: uuid.UUID, question: str) -> list[uuid.UUID]:
    expression = build_fts_query(question)
    if not expression:
        return []
    with tenant_scope(TENANT_A):
        hits = ChunkRepository().search_fts(expression, kb_id=kb_id, top_k=40)
    return [hit.document_id for hit in hits]


class TestDeletedDocumentIsUnretrievable:
    def test_vector_retrieval_stops_returning_it(self, tenant: None) -> None:
        """刪除後，那份文件的 chunk 不能再出現在向量檢索的結果裡。

        同一次查詢**還是要找得到另一份文件**——只驗「回空」的話，一個把整個 KB 弄壞
        的改動也會讓這條測試變綠。
        """
        kb_id, doomed, kept = _kb_with_two_documents()
        assert doomed in _vector_hits(kb_id, _SECRET), "前提：刪除前找得到"

        DocumentService().delete(TENANT_A, doomed)

        hits = _vector_hits(kb_id, _SECRET)
        assert doomed not in hits, "已刪除的文件仍會被檢索並引用（審計 H1）"
        assert kept in hits, "同一個 KB 裡沒被刪的文件必須照常查得到"

    def test_full_text_search_stops_returning_it(self, tenant: None) -> None:
        """FTS 是另一條手寫 SQL 的過濾條件，改了 ORM 那路不會讓它跟著對。"""
        kb_id, doomed, _ = _kb_with_two_documents()
        assert _fts_hits(kb_id, "ES256 是什麼") == [doomed], "前提：刪除前字面比對命中"

        DocumentService().delete(TENANT_A, doomed)

        assert _fts_hits(kb_id, "ES256 是什麼") == []

    def test_its_chunks_are_marked_superseded_not_hard_deleted(self, tenant: None) -> None:
        """標記而不是硬刪：硬刪要級聯 embedding，在請求路徑上會鎖表（05 §5.4）。

        ``superseded`` 是兩路 partial index 逐字認得的條件，因此標記就等於下架。
        """
        _, doomed, kept = _kb_with_two_documents()

        DocumentService().delete(TENANT_A, doomed)

        with tenant_scope(TENANT_A):
            assert Chunk.objects.filter(document_id=doomed, superseded=True).count() == 1
            assert Chunk.objects.filter(document_id=kept, superseded=False).count() == 1

    def test_the_daily_cleanup_job_hard_deletes_them(self, tenant: None) -> None:
        """標記完要有人收尾——否則只是把儲存成本往後推。

        既有的 `ChunkCleanupService` 清的是「ready 文件的 superseded chunk」，刪除留下
        的正好是這一種，不必為它新增第二個清理器。
        """
        _, doomed, kept = _kb_with_two_documents(status="ready")
        DocumentService().delete(TENANT_A, doomed)

        purged = ChunkCleanupService().purge_superseded(TENANT_A)

        assert purged == 1
        with tenant_scope(TENANT_A):
            assert not Chunk.objects.filter(document_id=doomed).exists()
            assert Chunk.objects.filter(document_id=kept).count() == 1

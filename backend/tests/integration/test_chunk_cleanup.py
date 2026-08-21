"""驗收：superseded chunk 的清理 job（06 §2.2「新版本算完 → 原子切換 → 清理舊版」，
1B 帶進 2A 的缺口②，2A-2b）。

re-ingest 之後舊版 chunk 標 `superseded=True` 留在 DB（1B-6），至今沒有人刪：
檢索靠 partial index 看不到它們，**但儲存與 embedding 都在付它們的錢**，且
`test_embedding_pipeline` 的註解早就寫著「舊版 chunk 即將被清理 job 硬刪（2A）」。

刪的條件是**文件已 ready**：新版本還在 embedding 時，舊版是唯一還在的完整資料
——那時清掉它，重跑失敗的文件會連舊的都沒有。

三件事錯了都不會有例外：

1. **刪錯邊**（現行版被刪）。檢索立刻回空，而文件狀態還是 ready。
2. **漏刪 embeddings**。`Embedding.chunk` 是 PROTECT——先刪 chunk 會被 DB 擋下；
   反過來只刪 embedding 不刪 chunk，錢省了一半，表繼續長。
3. **跨租戶**。清理是逐租戶的維運迴圈，少了 tenant context 這種 job 一列都刪
   不到（RLS fail closed）——症狀是 job 全綠、表照樣長。
"""

from __future__ import annotations

import uuid

import pytest
from services.knowledge.cleanup import ChunkCleanupService

from apps.knowledge.models import Chunk, Embedding
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import (
    make_chunk,
    make_document,
    make_embedding,
    make_knowledge_base,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def tenants() -> None:
    for tenant_id, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=slug)


def _document_with_chunks(
    tenant_id: uuid.UUID, *, status: str = "ready", superseded: int = 2, current: int = 2
) -> uuid.UUID:
    """一份文件：`current` 個現行 chunk（v2）＋ `superseded` 個舊版 chunk（v1），
    每個 chunk 各一份向量。"""
    with tenant_scope(tenant_id):
        kb = make_knowledge_base(tenant_id=tenant_id)
        document = make_document(kb=kb, status=status, doc_version=2)
        for seq in range(superseded):
            chunk = make_chunk(
                document=document, seq=seq, doc_version=1, superseded=True, content=f"舊 {seq}"
            )
            make_embedding(chunk=chunk)
        for seq in range(current):
            chunk = make_chunk(
                document=document, seq=seq, doc_version=2, superseded=False, content=f"新 {seq}"
            )
            make_embedding(chunk=chunk)
        return uuid.UUID(str(document.id))


def _counts(tenant_id: uuid.UUID) -> tuple[int, int, int]:
    """(superseded chunk 數, 現行 chunk 數, embedding 總數)。"""
    with tenant_scope(tenant_id):
        return (
            Chunk.objects.filter(superseded=True).count(),
            Chunk.objects.filter(superseded=False).count(),
            Embedding.objects.count(),
        )


class TestPurge:
    def test_superseded_chunks_of_ready_documents_are_hard_deleted(self, tenants: None) -> None:
        _document_with_chunks(TENANT_A, status="ready")

        purged = ChunkCleanupService().purge_superseded(TENANT_A)

        assert purged == 2
        superseded, current, embeddings = _counts(TENANT_A)
        assert superseded == 0, "舊版要真的消失（硬刪，不是再標一次）"
        assert current == 2, "現行版一根汗毛都不能少"
        assert embeddings == 2, "舊版的向量要一起刪（PROTECT 之下順序是先向量後 chunk）"

    def test_documents_still_processing_keep_their_old_chunks(self, tenants: None) -> None:
        """embedding 中的文件：舊版是唯一完整的資料，重跑失敗時它是最後的退路。"""
        _document_with_chunks(TENANT_A, status="embedding")

        purged = ChunkCleanupService().purge_superseded(TENANT_A)

        assert purged == 0
        superseded, _, embeddings = _counts(TENANT_A)
        assert superseded == 2
        assert embeddings == 4

    def test_it_is_idempotent(self, tenants: None) -> None:
        _document_with_chunks(TENANT_A, status="ready")
        service = ChunkCleanupService()
        service.purge_superseded(TENANT_A)

        assert service.purge_superseded(TENANT_A) == 0

    def test_other_tenants_are_untouched(self, tenants: None) -> None:
        _document_with_chunks(TENANT_A, status="ready")
        _document_with_chunks(TENANT_B, status="ready")

        ChunkCleanupService().purge_superseded(TENANT_A)

        superseded_b, current_b, embeddings_b = _counts(TENANT_B)
        assert (superseded_b, current_b, embeddings_b) == (2, 2, 4)

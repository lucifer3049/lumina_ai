"""superseded chunk 的清理（06 §2.2 的第三步「清理舊版」，1B 缺口②，2A-2b）。

re-ingest 把舊版 chunk 標 `superseded=True` 留在 DB（1B-6）：檢索的 partial index
看不到它們，但儲存與向量都在付它們的錢。這裡每天把 **ready 文件**的舊版硬刪——
還在處理中的文件不碰（舊版是它最後的退路）。

刪除範圍刻意保守：只有「新版本已完整上線」這一種情況。任何拿不準的狀態
（failed、embedding、uploaded）都留著——留著的代價是幾 MB，刪錯的代價是資料。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.utils import timezone

from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import TenantDirectoryRepository
from repositories.knowledge import (
    ChunkRepository,
    EmbeddingRepository,
    KbReindexJobRepository,
    KnowledgeBaseRepository,
)

logger = get_logger(__name__)

__all__ = ["ChunkCleanupService", "OldEmbeddingCleanupService"]


class ChunkCleanupService:
    def __init__(
        self,
        *,
        chunks: ChunkRepository | None = None,
        directory: TenantDirectoryRepository | None = None,
    ) -> None:
        self._chunks = chunks or ChunkRepository()
        self._directory = directory or TenantDirectoryRepository()

    def purge_superseded(self, tenant_id: uuid.UUID) -> int:
        """清一個租戶；回傳刪掉的 chunk 數。"""
        with tenant_context(tenant_id), unit_of_work():
            purged = self._chunks.purge_superseded_of_ready()
        if purged:
            logger.info("superseded_chunks_purged", tenant_id=str(tenant_id), count=purged)
        return purged

    def purge_all(self) -> int:
        """逐 active 租戶清理（Beat 每日）；回傳刪掉的總數。
        單一租戶失敗不中斷整輪（理由同對帳的 reconcile_all）。"""
        total = 0
        for tenant_id in self._directory.active_tenant_ids():
            try:
                total += self.purge_superseded(tenant_id)
            except Exception:
                logger.exception("chunk_cleanup_failed", tenant_id=str(tenant_id))
        return total


class OldEmbeddingCleanupService:
    """重建的第 4 步：觀察期過了才刪舊版向量（06 §2.2，2B-6）。

    「觀察期（可回退）」的**可回退**具體就是指這些向量還在：把 KB 的
    ``embedding_model`` / ``embedding_version`` 改回去，檢索當場回到重建前的行為。
    窗內就刪的話，回退只剩「再重建一次」，而那是整庫重算的錢與時間。

    **兩個條件缺一不可，而且錯了都沒有例外**：

    1. **只認切換過的 job**（``status=completed`` 且有 ``switched_at``）。以「不等於
       KB 現行版本」為條件的話，它會刪掉**剛算好、還沒切換**的那一批——那個 job 於是
       永遠到不了 100%，而且每一輪都重算一次、每一輪都被刪一次。
    2. **窗從 `switched_at` 起算**，不是 `created_at`：後者等於「重建跑得愈久，可回退
       的時間愈短」，而跑得久的正是最該留退路的那些。
    """

    def __init__(
        self,
        *,
        jobs: KbReindexJobRepository | None = None,
        knowledge_bases: KnowledgeBaseRepository | None = None,
        embeddings: EmbeddingRepository | None = None,
        directory: TenantDirectoryRepository | None = None,
    ) -> None:
        self._jobs = jobs or KbReindexJobRepository()
        self._knowledge_bases = knowledge_bases or KnowledgeBaseRepository()
        self._embeddings = embeddings or EmbeddingRepository()
        self._directory = directory or TenantDirectoryRepository()

    def purge_switched(self, tenant_id: uuid.UUID) -> int:
        """清一個租戶；回傳刪掉的向量數。"""
        settings = get_app_settings()
        cutoff = timezone.now() - timedelta(days=settings.reindex_rollback_window_days)

        purged = 0
        with tenant_context(tenant_id), unit_of_work():
            for job in self._jobs.switched_before(cutoff, limit=settings.reindex_purge_batch_size):
                kb = self._knowledge_bases.get_by_id(job.kb_id)
                if kb is None:
                    # KB 已經被刪掉：向量的清理走 `DeletedKnowledgePurgeService` 的
                    # 級聯（那一支認得「KB 已刪」這個形狀），這裡只把 job 標記掉，
                    # 免得它每天被掃出來一次。
                    self._jobs.update(job.id, purged_at=timezone.now())
                    continue
                # **保留的是 KB 的現行值，不是 job 的目標值**：切換之後兩者相同，
                # 但若有人手動回退過，現行值才是還在服務查詢的那一版。
                purged += self._embeddings.purge_other_versions(
                    kb_id=kb.id,
                    keep_model=str(kb.embedding_model),
                    keep_embedding_version=int(kb.embedding_version),
                )
                self._jobs.update(job.id, purged_at=timezone.now())

        if purged:
            logger.info("old_embeddings_purged", tenant_id=str(tenant_id), count=purged)
        return purged

    def purge_all(self) -> int:
        """逐 active 租戶清理（Beat 每日，與 superseded chunk 同一個 task）。"""
        total = 0
        for tenant_id in self._directory.active_tenant_ids():
            try:
                total += self.purge_switched(tenant_id)
            except Exception:
                logger.exception("old_embedding_cleanup_failed", tenant_id=str(tenant_id))
        return total

"""superseded chunk 的清理（06 §2.2 的第三步「清理舊版」，1B 缺口②，2A-2b）。

re-ingest 把舊版 chunk 標 `superseded=True` 留在 DB（1B-6）：檢索的 partial index
看不到它們，但儲存與向量都在付它們的錢。這裡每天把 **ready 文件**的舊版硬刪——
還在處理中的文件不碰（舊版是它最後的退路）。

刪除範圍刻意保守：只有「新版本已完整上線」這一種情況。任何拿不準的狀態
（failed、embedding、uploaded）都留著——留著的代價是幾 MB，刪錯的代價是資料。
"""

from __future__ import annotations

import uuid

from config.logging import get_logger
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import TenantDirectoryRepository
from repositories.knowledge import ChunkRepository

logger = get_logger(__name__)

__all__ = ["ChunkCleanupService"]


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

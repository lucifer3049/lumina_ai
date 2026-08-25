"""保留窗到期的**硬刪**——KB、文件、chunk、向量、ETL job 與物件（05 §5.4）。

軟刪除從 1B 起就承諾「30 天後由清理 job 硬刪」——`repositories/knowledge.py` 的模組
docstring、`KnowledgeBaseService.delete`、`DocumentService.delete` 三處都這麼寫，而
那個 job 到 2B 為止**不存在**。後果只增不減：刪掉的文件的 chunk、向量與 MinIO 物件
全部留著，KB 級刪除更是連一筆 `deleted_at` 都不會寫到文件上（`KnowledgeBaseService`
刻意不逐列標記），所以那些資料連「已經被刪」的痕跡都沒有。

與 `ChunkCleanupService` 是**兩件不同的事**，不要合併：那一支清的是「還活著的文件的
舊版本」（re-ingest 的殘留），條件是 `superseded`；這一支清的是「文件本身要消失」，
現行版一起刪。前者天天跑、量小；後者有 30 天的保留窗，且不可逆。

**順序是 05 §5.4 的那一條，錯了會被 DB 擋下（FK 全是 PROTECT）**：
向量 → chunk → etl_job → 物件 → 文件 → KB。被擋下不是最糟的情況——最糟的是順序對
但漏一步，那時 job 每天都跑、每天都在同一批文件上失敗，而表繼續長。

**物件刪在 DB 之前**，且與 DB 不同交易：反過來（先刪列再刪物件）時，物件那一步失敗
就永遠沒有人知道那個 key 是什麼——列已經不在了。先刪物件的代價只是「刪了物件但列還
在」，而下一輪會把它撿回來重做（`delete_object` 對不存在的 key 是冪等的）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from core.object_storage import delete_object
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import TenantDirectoryRepository
from repositories.knowledge import (
    ChunkRepository,
    DocumentRepository,
    EtlJobRepository,
    KnowledgeBaseRepository,
)

logger = get_logger(__name__)

__all__ = ["DeletedKnowledgePurgeService", "KnowledgePurgeCounts"]


@dataclass(frozen=True, slots=True)
class KnowledgePurgeCounts:
    """一輪清理刪掉的量。**四個數字分開記**：只回一個總數的話，「物件沒刪到」與
    「chunk 沒刪到」在 log 裡長得一模一樣，而兩者的成因完全不同。"""

    knowledge_bases: int = 0
    documents: int = 0
    chunks: int = 0
    objects: int = 0

    def __add__(self, other: KnowledgePurgeCounts) -> KnowledgePurgeCounts:
        return KnowledgePurgeCounts(
            knowledge_bases=self.knowledge_bases + other.knowledge_bases,
            documents=self.documents + other.documents,
            chunks=self.chunks + other.chunks,
            objects=self.objects + other.objects,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "knowledge_bases": self.knowledge_bases,
            "documents": self.documents,
            "chunks": self.chunks,
            "objects": self.objects,
        }


class DeletedKnowledgePurgeService:
    def __init__(
        self,
        *,
        knowledge_bases: KnowledgeBaseRepository | None = None,
        documents: DocumentRepository | None = None,
        chunks: ChunkRepository | None = None,
        etl_jobs: EtlJobRepository | None = None,
        directory: TenantDirectoryRepository | None = None,
    ) -> None:
        self._knowledge_bases = knowledge_bases or KnowledgeBaseRepository()
        self._documents = documents or DocumentRepository()
        self._chunks = chunks or ChunkRepository()
        self._etl_jobs = etl_jobs or EtlJobRepository()
        self._directory = directory or TenantDirectoryRepository()

    def purge_for_tenant(
        self, tenant_id: uuid.UUID, *, deleted_before: datetime | None = None
    ) -> KnowledgePurgeCounts:
        """清一個租戶的一批；回傳刪掉的量。

        **一批就好，不迴圈到清空**：批次上限（`retention_purge_batch_size`）的目的
        是不要讓單一租戶的積欠把整輪維運窗吃光，在這裡加一個 while 等於把上限抵銷掉。
        沒清完的下一輪繼續——job 每天都跑，而這些資料已經在保留窗外躺了 30 天。

        ``deleted_before`` 覆寫保留窗，**只給維運指令用**（`purge_eval_knowledge`）。
        排程一律不傳，走 `retention_purge_after_days`——保留窗的意義是「使用者可能
        後悔」，讓排程能繞過它等於讓那個窗變成裝飾。
        """
        settings = get_app_settings()
        cutoff = deleted_before or (
            timezone.now() - timedelta(days=settings.retention_purge_after_days)
        )
        limit = settings.retention_purge_batch_size

        with tenant_context(tenant_id), unit_of_work():
            targets = self._documents.purgeable(cutoff, limit=limit)
            document_ids = [document_id for document_id, _ in targets]
            chunks = self._chunks.purge_for_documents(document_ids)
            self._etl_jobs.purge_for_documents(document_ids)

        with tenant_context(tenant_id):
            objects = self._purge_objects([key for _, key in targets])

        with tenant_context(tenant_id), unit_of_work():
            documents = self._documents.hard_delete(document_ids)
            # KB 最後，而且**只刪已經沒有文件的**：`Document.kb` 是 PROTECT，還有文件
            # 的 KB 會被 DB 擋下——那批文件可能還沒輪到（批次上限），下一輪才會清。
            empty = [
                kb_id
                for kb_id in self._knowledge_bases.deleted_before(cutoff, limit=limit)
                if self._documents.count_all_in_kb(kb_id) == 0
            ]
            knowledge_bases = self._knowledge_bases.hard_delete(empty)

        counts = KnowledgePurgeCounts(
            knowledge_bases=knowledge_bases,
            documents=documents,
            chunks=chunks,
            objects=objects,
        )
        if documents or knowledge_bases:
            logger.info("knowledge_retention_purged", tenant_id=str(tenant_id), **counts.as_dict())
        return counts

    def purge_all(self) -> KnowledgePurgeCounts:
        """逐 active 租戶清理（Beat 每日）。單一租戶失敗不中斷整輪（同 `purge_all`
        的其他維運迴圈）——一個租戶的 FK 卡住不該讓其他租戶的資料多留一天。"""
        total = KnowledgePurgeCounts()
        for tenant_id in self._directory.active_tenant_ids():
            try:
                total += self.purge_for_tenant(tenant_id)
            except Exception:
                logger.exception("knowledge_retention_purge_failed", tenant_id=str(tenant_id))
        return total

    def _purge_objects(self, keys: list[str]) -> int:
        """刪物件；回傳成功的數量。**單一 key 失敗不中斷**——刪不掉的是幾 MB 的孤兒
        位元組，而中斷會讓後面那些文件連 DB 的列都留著。"""
        deleted = 0
        for key in keys:
            try:
                delete_object(key)
            except Exception:
                logger.warning("retention_object_delete_failed", storage_key=key, exc_info=True)
                continue
            deleted += 1
        return deleted

"""保留窗到期的**硬刪**——對話、訊息與記憶摘要（05 §5.4）。

與 `services/knowledge/purge.py` 是同一件維運工作的另一半，形狀刻意一致（同一個保留
窗設定、同一個逐租戶迴圈、同一個「一輪一批」的節制）。分成兩支而不是一支的理由是
bounded context（ADR-006）：knowledge 與 conversation 之間不該互相 import，兩者的組合
點在 worker 的 task——那本來就是最外層。

`ConversationService.delete` 的 docstring 從 1D 起就寫著「硬刪由清理 job 在 30 天後
做」，而那個 job 不存在。對話的量級與成本都遠小於 KB／文件，但**訊息是使用者內容**
——留著的是保留合規問題，不是磁碟問題。

順序同樣被 PROTECT 釘死：摘要與訊息 → 對話。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.conversation import (
    ConversationRepository,
    MemorySnapshotRepository,
    MessageRepository,
)
from repositories.identity import TenantDirectoryRepository

logger = get_logger(__name__)

__all__ = ["ConversationPurgeCounts", "DeletedConversationPurgeService"]


@dataclass(frozen=True, slots=True)
class ConversationPurgeCounts:
    conversations: int = 0
    messages: int = 0

    def __add__(self, other: ConversationPurgeCounts) -> ConversationPurgeCounts:
        return ConversationPurgeCounts(
            conversations=self.conversations + other.conversations,
            messages=self.messages + other.messages,
        )

    def as_dict(self) -> dict[str, int]:
        return {"conversations": self.conversations, "messages": self.messages}


class DeletedConversationPurgeService:
    def __init__(
        self,
        *,
        conversations: ConversationRepository | None = None,
        messages: MessageRepository | None = None,
        snapshots: MemorySnapshotRepository | None = None,
        directory: TenantDirectoryRepository | None = None,
    ) -> None:
        self._conversations = conversations or ConversationRepository()
        self._messages = messages or MessageRepository()
        self._snapshots = snapshots or MemorySnapshotRepository()
        self._directory = directory or TenantDirectoryRepository()

    def purge_for_tenant(self, tenant_id: uuid.UUID) -> ConversationPurgeCounts:
        """清一個租戶的一批；回傳刪掉的量。

        全部在**同一個交易**裡（與 knowledge 那一支不同）：這裡沒有物件儲存那種
        「交易外的外部系統」，而訊息與對話分兩個交易刪的話，中間崩潰會留下一場沒有
        訊息的空對話——而它已經看不到了，沒有人會發現。
        """
        settings = get_app_settings()
        cutoff = timezone.now() - timedelta(days=settings.retention_purge_after_days)
        limit = settings.retention_purge_batch_size

        with tenant_context(tenant_id), unit_of_work():
            conversation_ids = self._conversations.deleted_before(cutoff, limit=limit)
            self._snapshots.purge_for_conversations(conversation_ids)
            messages = self._messages.purge_for_conversations(conversation_ids)
            conversations = self._conversations.hard_delete(conversation_ids)

        counts = ConversationPurgeCounts(conversations=conversations, messages=messages)
        if conversations:
            logger.info(
                "conversation_retention_purged", tenant_id=str(tenant_id), **counts.as_dict()
            )
        return counts

    def purge_all(self) -> ConversationPurgeCounts:
        """逐 active 租戶清理（Beat 每日）。單一租戶失敗不中斷整輪。"""
        total = ConversationPurgeCounts()
        for tenant_id in self._directory.active_tenant_ids():
            try:
                total += self.purge_for_tenant(tenant_id)
            except Exception:
                logger.exception("conversation_retention_purge_failed", tenant_id=str(tenant_id))
        return total

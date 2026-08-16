"""Conversation 的 Repository —— 租戶隔離的第一道防線（鐵則 4）。

第二道是 RLS policy（`apps/conversation/migrations/0002_rls.py` 與每個分區）。

**三個查詢語意在這裡定案，錯了都不會報錯**：

1. :meth:`MessageRepository.for_conversation` **必須有明確 ORDER BY**。分區表可能
   平行掃描多個分區再合併，順序完全不保證——而症狀是跨月的對話讀回來前後顛倒，
   同月的完全正常，所以本機開發期測不出來。
2. :meth:`MessageRepository.append` 是 `message_count` 與 `last_message_at` **唯一的
   寫入點**。05 §6 說那兩欄的反正規化「前提是寫入點單一」；散開之後總有一條路徑忘了
   更新，而症狀是對話列表的排序與則數與實際內容不符，沒有錯誤訊息。
3. :meth:`MemorySnapshotRepository.latest_for` 取**版本最大**的那一份。取錯版本的
   後果是 LLM 拿到過期的對話摘要，回答會參照早已被修正的內容——看起來像模型在胡說。
"""

from __future__ import annotations

import uuid
from typing import Any

from django.db.models import F
from django.utils import timezone

from apps.conversation.models import Conversation, MemorySnapshot, Message
from core.tenant import get_current_tenant_id
from repositories.base import SoftDeletableRepository, TenantScopedRepository


class ConversationRepository(SoftDeletableRepository[Conversation]):
    """對話。軟刪除實體（05 §5.4）。"""

    model = Conversation

    def get_by_id(self, conversation_id: uuid.UUID) -> Conversation | None:
        """不存在或屬於別的租戶都回 ``None``——API 層一律轉 404（09 §2.3）。"""
        return self.get_queryset().filter(id=conversation_id).first()

    def list_for_tenant(self, *, user_id: uuid.UUID | None = None) -> list[Conversation]:
        """對話列表，最近有訊息的排前面。

        排序條件與 ``ix_conv_tenant_user_recent`` 逐字對應（含 partial 的
        ``deleted_at IS NULL``），查詢才吃得到那個索引。``last_message_at`` 可能是
        NULL（剛建立、還沒發言），``F(...).desc(nulls_last=True)`` 讓那些排在後面
        而不是最前面——預設的 NULLS FIRST 會讓空對話霸佔列表頂端。
        """
        queryset = self.get_queryset()
        if user_id is not None:
            queryset = queryset.filter(user_id=user_id)
        return list(queryset.order_by(F("last_message_at").desc(nulls_last=True), "-created_at"))

    def create(
        self,
        *,
        user_id: uuid.UUID,
        title: str = "",
        kb_ids: list[uuid.UUID] | None = None,
        prompt_key: str = "",
    ) -> Conversation:
        return Conversation.objects.create(
            tenant_id=get_current_tenant_id(operation="ConversationRepository.create"),
            user_id=user_id,
            title=title,
            kb_ids=list(kb_ids or []),
            prompt_key=prompt_key,
        )

    def update(self, conversation_id: uuid.UUID, **fields: object) -> int:
        """部分更新——只寫呼叫端明確給的欄位（理由同 KnowledgeBaseRepository.update）。"""
        return self.get_queryset().filter(id=conversation_id).update(**fields)


class MessageRepository(TenantScopedRepository[Message]):
    """訊息。**分區表**（05 §5.2）——順序與寫入點見模組 docstring。"""

    model = Message

    def for_conversation(
        self, conversation_id: uuid.UUID, *, limit: int | None = None
    ) -> list[Message]:
        """一場對話的訊息，**依時間排序**（見模組 docstring 第 1 點）。

        ``limit`` 取的是**最新的 N 則**（1D-5 的記憶視窗需要），但回傳仍照時間正序
        ——LLM 讀的是對話，倒著給它會直接改變語意。
        """
        queryset = self.get_queryset().filter(conversation_id=conversation_id)
        if limit is None:
            return list(queryset.order_by("created_at"))
        newest = list(queryset.order_by("-created_at")[:limit])
        return sorted(newest, key=lambda message: message.created_at)

    def append(
        self,
        *,
        conversation_id: uuid.UUID,
        role: str,
        content: str = "",
        status: str = "completed",
        **fields: Any,
    ) -> Message:
        """寫一則訊息，並同步對話的反正規化欄位。

        **兩件事必須在同一個交易裡**（呼叫端的 `unit_of_work` 提供）：訊息落地與計數
        更新。分開的話，中途失敗會留下「訊息在、計數沒加」的狀態，而那不會有任何症狀
        ——只是列表排序永遠差一則。

        計數用 ``F('message_count') + 1`` 而不是讀出來加一：兩個請求同時發言時，
        read-modify-write 會讓其中一次的增量消失。
        """
        tenant_id = get_current_tenant_id(operation="MessageRepository.append")
        message = Message.objects.create(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            status=status,
            **fields,
        )
        Conversation.objects.filter(tenant_id=tenant_id, id=conversation_id).update(
            message_count=F("message_count") + 1,
            last_message_at=message.created_at,
            updated_at=timezone.now(),
        )
        return message

    def set_status(
        self,
        message_id: uuid.UUID,
        *,
        status: str,
        error: dict[str, Any] | None = None,
        **fields: Any,
    ) -> int:
        """串流結束時收尾（05 §3.4 的 streaming → completed / interrupted / failed）。

        1D-4 會用它把中斷的訊息標成 `interrupted` 並保住已產生的部分——那是 09 附錄 A
        的 `STREAM_INTERRUPTED` 要求的「partial 已保存」。
        """
        return (
            self.get_queryset().filter(id=message_id).update(status=status, error=error, **fields)
        )


class MemorySnapshotRepository(TenantScopedRepository[MemorySnapshot]):
    model = MemorySnapshot

    def latest_for(self, conversation_id: uuid.UUID) -> MemorySnapshot | None:
        """版本最大的那一份；沒有摘要**不是錯誤**（新對話本來就沒有）。"""
        return (
            self.get_queryset().filter(conversation_id=conversation_id).order_by("-version").first()
        )

    def create(
        self,
        *,
        conversation_id: uuid.UUID,
        summary: str,
        token_count: int = 0,
        upto_message_id: uuid.UUID | None = None,
        version: int | None = None,
    ) -> MemorySnapshot:
        """新增一版摘要。``version`` 不給時取「現有最大 + 1」。

        遞增而不是覆寫（05 §3.4）：摘要是 LLM 產出的、會錯，而「這次回答依據的是哪一版
        摘要」在事後追查時是唯一的線索。
        """
        if version is None:
            latest = self.latest_for(conversation_id)
            version = (latest.version + 1) if latest else 1
        return MemorySnapshot.objects.create(
            tenant_id=get_current_tenant_id(operation="MemorySnapshotRepository.create"),
            conversation_id=conversation_id,
            summary=summary,
            token_count=token_count,
            upto_message_id=upto_message_id,
            version=version,
        )

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
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from django.db.models import F, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.conversation.models import Conversation, MemorySnapshot, Message
from common.cursors import CursorError
from core.tenant import get_current_tenant_id
from repositories.base import SoftDeletableRepository, TenantScopedRepository


def _cursor_key(cursor: dict[str, Any]) -> tuple[datetime, uuid.UUID]:
    """游標 dict → (排序鍵, id)。內容壞掉一律 `CursorError`。

    游標可能來自舊版前端存的 localStorage、被截斷的網址、或使用者手改——因此
    **不能假設欄位存在或型別正確**，而錯誤要是呼叫端轉得成 422 的那一種。
    """
    try:
        return datetime.fromisoformat(str(cursor["k"])), uuid.UUID(str(cursor["id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise CursorError("游標內容不正確") from exc


def _split_page[T](
    rows: list[T], *, limit: int, cursor_of: Callable[[T], dict[str, Any]]
) -> tuple[list[T], dict[str, Any] | None]:
    """把「多取一筆」的結果切成一頁 + 下一頁的游標。

    **多取一筆是判斷「還有沒有下一頁」唯一可靠的方法**。用 `count()` 另外查一次的話，
    兩次查詢之間新增的資料會讓「有沒有下一頁」與實際內容對不上；而只憑「這頁滿了」
    來推斷，會在資料剛好是頁大小整數倍時多給一個永遠回空清單的游標。

    `cursor_of` 由呼叫端提供而不是在這裡讀欄位：排序鍵每個查詢不同，而
    `Conversation` 的鍵是 `COALESCE(last_message_at, created_at)`——在 Python 端算
    （`a or b`）與 SQL 的 COALESCE 等價，且不必把 annotate 出來的欄位帶進型別系統。
    """
    if len(rows) <= limit:
        return rows, None
    page = rows[:limit]
    return page, cursor_of(page[-1])


class ConversationRepository(SoftDeletableRepository[Conversation]):
    """對話。軟刪除實體（05 §5.4）。"""

    model = Conversation

    def get_by_id(self, conversation_id: uuid.UUID) -> Conversation | None:
        """不存在或屬於別的租戶都回 ``None``——API 層一律轉 404（09 §2.3）。"""
        return self.get_queryset().filter(id=conversation_id).first()

    def page_for_user(
        self, *, user_id: uuid.UUID, limit: int, cursor: dict[str, Any] | None = None
    ) -> tuple[list[Conversation], dict[str, Any] | None]:
        """使用者自己的對話，一頁；回傳 (這一頁, 下一頁的游標鍵)。

        **排序鍵是 ``COALESCE(last_message_at, created_at)``**，與
        ``ix_conv_tenant_user_recent`` 逐字對應（見 model 的 Meta 註解）。用它而不是
        ``last_message_at`` 有兩個理由：NULL（剛建立、還沒發言）要排在後面，而
        PostgreSQL 的 DESC 預設是 NULLS FIRST；以及它讓游標只需要一個排序鍵。

        **`id` 是必要的第二排序鍵**：同一毫秒建立的兩場對話若只比第一個鍵，翻頁時
        會互相擠掉——那是分頁最常見的漏資料方式，而且只在資料剛好跨頁時出現。

        `user_id` 是**必填**：對話是擁有者制（09 §2.4），列表漏掉這個條件的話，
        使用者會在自己的列表上看到別人的對話標題——而標題本身就已經是洩漏。
        RLS 在這裡幫不上忙，它是租戶級的。
        """
        ordering = Coalesce("last_message_at", "created_at")
        queryset = self.get_queryset().filter(user_id=user_id).annotate(_sort_key=ordering)
        if cursor is not None:
            key, last_id = _cursor_key(cursor)
            queryset = queryset.filter(Q(_sort_key__lt=key) | Q(_sort_key=key, id__lt=last_id))
        rows = cast("list[Conversation]", list(queryset.order_by("-_sort_key", "-id")[: limit + 1]))
        return _split_page(
            rows,
            limit=limit,
            # `a or b` 與 SQL 的 COALESCE 等價——在 Python 端算，游標值就不必經過
            # annotate 出來的欄位（那個欄位在型別系統裡不存在）。
            cursor_of=lambda row: {
                "k": (row.last_message_at or row.created_at).isoformat(),
                "id": str(row.id),
            },
        )

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
            return list(queryset.order_by("created_at", "id"))
        newest = list(queryset.order_by("-created_at", "-id")[:limit])
        return sorted(newest, key=lambda message: (message.created_at, str(message.id)))

    def assistant_count_since(self, since: Any) -> int:
        """當期 assistant 訊息數——messages_day 對帳的事實來源（2A-2b）。

        用訊息表而不是 usage_logs：被中止且 provider 未回報的回合沒有 usage 列，
        但那一輪確實發生過、也確實在開場時扣了額度。
        """
        return self.get_queryset().filter(role="assistant", created_at__gte=since).count()

    def page_for_conversation(
        self, conversation_id: uuid.UUID, *, limit: int, cursor: dict[str, Any] | None = None
    ) -> tuple[list[Message], dict[str, Any] | None]:
        """一頁訊息，**時間正序**；回傳 (這一頁, 下一頁的游標鍵)。

        正序而不是倒序：對話要照順序讀，而 1D-5 會用同一條路徑組 context——倒著給
        LLM 會直接改變語意。前端要「最新的在下面」本來就是正序。

        `id` 是必要的第二排序鍵，理由同 `page_for_user`。
        """
        queryset = self.get_queryset().filter(conversation_id=conversation_id)
        if cursor is not None:
            key, last_id = _cursor_key(cursor)
            queryset = queryset.filter(Q(created_at__gt=key) | Q(created_at=key, id__gt=last_id))
        rows = list(queryset.order_by("created_at", "id")[: limit + 1])
        return _split_page(
            rows,
            limit=limit,
            cursor_of=lambda row: {"k": row.created_at.isoformat(), "id": str(row.id)},
        )

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

"""ConversationService —— 對話的 CRUD 與訊息讀取（09 §2.4）。

**這一層扛的是「擁有者判定」**，而它與前面幾包的權限檢查是不同的東西：

- `chat:use` 由 API 層的 `RequireScope` 檢查——那是「這個人能不能用聊天」。
- **擁有者**由這裡檢查——那是「這場對話是不是他的」。

**RLS 擋不住第二件事。** RLS 是租戶級的隔離：同一個租戶裡的兩個使用者，policy 看到的
`app.tenant_id` 一模一樣，兩邊的資料互相都在範圍內。前面幾包養成的「漏寫 filter 也有
RLS 兜底」的直覺在這裡是錯的——**這一層沒有第二道防線**。

而對話內容比文件更敏感：文件是租戶放進系統的東西，對話是「這個人問了什麼」。

**找不到與沒有權限一律回 `NotFoundError`**（09 §2.3 的資源類規則）。回 403 等於承認
那個 id 存在，可以拿來掃出別人有哪些對話——而對話標題本身就已經是資訊。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from common.cursors import CursorError, decode_cursor, encode_cursor
from core.exceptions import NotFoundError, ValidationFailedError
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.conversation import ConversationRepository, MessageRepository
from repositories.knowledge import KnowledgeBaseRepository

# 09 §1.1：預設 20、上限 100。
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# PATCH 允許改的欄位（09 §2.4：改名／釘選／封存）。
#
# **白名單而不是黑名單**：直接把 body 展開成 update 的話，client 送
# `{"tenant_id": "..."}` 就能把對話搬到別的租戶——而那不會有任何錯誤。
_PATCHABLE = frozenset({"title", "pinned", "status"})


@dataclass(frozen=True)
class ConversationView:
    id: uuid.UUID
    title: str
    kb_ids: list[uuid.UUID]
    prompt_key: str
    status: str
    pinned: bool
    message_count: int
    last_message_at: datetime | None


@dataclass(frozen=True)
class MessageView:
    id: uuid.UUID
    role: str
    content: str
    citations: list[dict[str, Any]]
    model: str
    status: str
    usage: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None


class ConversationService:
    def __init__(
        self,
        *,
        conversations: ConversationRepository | None = None,
        messages: MessageRepository | None = None,
        knowledge_bases: KnowledgeBaseRepository | None = None,
    ) -> None:
        self._conversations = conversations or ConversationRepository()
        self._messages = messages or MessageRepository()
        self._knowledge_bases = knowledge_bases or KnowledgeBaseRepository()

    # ── 對話 ────────────────────────────────────────────────

    def create(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        title: str = "",
        kb_ids: list[uuid.UUID] | None = None,
        prompt_key: str = "",
    ) -> ConversationView:
        """建立一場對話。`kb_ids` 必須都是**本租戶真的存在**的知識庫（1D-5）。

        不驗的話，打錯一個字會安靜地毀掉整場對話：檢索每一輪都跳過那個 id、回答每一次
        都是「知識庫中找不到相關內容」，而使用者看到的是「這個 AI 很笨」而不是「我填錯
        了」。唯一的線索是後台的 `rag_kb_unavailable`，那要有人想到去翻。

        **只在這裡擋，生成時不擋。** 對話進行到一半知識庫被刪掉是相反的情境——那時整輪
        失敗會讓那場對話從此每一次發言都失敗，正確的行為是跳過並照常回答
        （`RetrievalService.retrieve_for_chat`）。填錯是當下就錯了，被刪是後來才變的。
        """
        with tenant_context(tenant_id), unit_of_work():
            self._require_knowledge_bases(kb_ids)
            conversation = self._conversations.create(
                user_id=user_id, title=title, kb_ids=kb_ids, prompt_key=prompt_key
            )
            return _conversation_view(conversation)

    def _require_knowledge_bases(self, kb_ids: list[uuid.UUID] | None) -> None:
        """**全有或全無。**

        悄悄濾掉查不到的那幾個等於「建出來了，但少查一個知識庫」——而那正是這道驗證
        要防的安靜降級。

        找不到與屬於別的租戶都回 `NotFoundError`（09 §2.3）：403 等於承認那個 id 存在，
        可以拿來掃出別的租戶有哪些知識庫。租戶過濾由 `get_queryset` 負責。
        """
        wanted = list(kb_ids or [])
        if not wanted:
            return
        found = {
            uuid.UUID(str(kb_id))
            # 一次查完，不逐個 `get_by_id`：後者是 N 次來回，而 `kb_ids` 是使用者給的
            # 清單——沒有上限時那就是 N 次查詢的放大器。
            for kb_id in self._knowledge_bases.get_queryset()
            .filter(id__in=wanted)
            .values_list("id", flat=True)
        }
        missing = [kb_id for kb_id in wanted if kb_id not in found]
        if missing:
            raise NotFoundError("知識庫不存在")

    def list_for_user(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> Page[ConversationView]:
        """**只列自己的**（擁有者制）。漏了這個條件，使用者會在自己的列表上看到
        別人的對話標題——而點進去才 404，標題已經洩漏了。"""
        with tenant_context(tenant_id), unit_of_work():
            rows, next_key = self._conversations.page_for_user(
                user_id=user_id, limit=limit, cursor=_decode(cursor)
            )
            return Page(
                items=[_conversation_view(row) for row in rows],
                next_cursor=encode_cursor(next_key) if next_key else None,
            )

    def get(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> ConversationView:
        with tenant_context(tenant_id), unit_of_work():
            return _conversation_view(self._require_own(user_id, conversation_id))

    def update(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        **fields: Any,
    ) -> ConversationView:
        """部分更新。

        `None` 代表「client 沒給這個欄位」而被濾掉——當成「設為空」寫回去的話，
        使用者改一次標題就會把釘選狀態清掉，而那不會有錯誤訊息。
        """
        changes = {
            key: value for key, value in fields.items() if key in _PATCHABLE and value is not None
        }
        with tenant_context(tenant_id), unit_of_work():
            self._require_own(user_id, conversation_id)
            if changes:
                self._conversations.update(conversation_id, **changes)
            refreshed = self._require_own(user_id, conversation_id)
            return _conversation_view(refreshed)

    def delete(self, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
        """軟刪除（05 §5.4）。硬刪由清理 job 在 30 天後做。"""
        with tenant_context(tenant_id), unit_of_work():
            self._require_own(user_id, conversation_id)
            self._conversations.soft_delete(conversation_id)

    # ── 訊息 ────────────────────────────────────────────────

    def list_messages(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> Page[MessageView]:
        """一頁訊息，**時間正序**。

        擁有者判定走與詳情**同一個** `_require_own`：對話擋住了但訊息沒擋，等於前門
        鎖了後門開著，而「知道 conversation id」對同租戶的人並不困難。
        """
        with tenant_context(tenant_id), unit_of_work():
            self._require_own(user_id, conversation_id)
            rows, next_key = self._messages.page_for_conversation(
                conversation_id, limit=limit, cursor=_decode(cursor)
            )
            return Page(
                items=[message_view(row) for row in rows],
                next_cursor=encode_cursor(next_key) if next_key else None,
            )

    # ── 輔助 ────────────────────────────────────────────────

    def _require_own(self, user_id: uuid.UUID, conversation_id: uuid.UUID) -> Any:
        """取得對話，**並確認它屬於這個使用者**。

        兩種失敗合併成 `NotFoundError`（09 §2.3）：不存在、以及存在但不是他的。
        分開回應的話，403 就等於承認「這個 id 存在」——那可以拿來掃出租戶內有哪些
        對話，而對話標題本身就是資訊。
        """
        conversation = self._conversations.get_by_id(conversation_id)
        if conversation is None or conversation.user_id != user_id:
            raise NotFoundError("對話不存在")
        return conversation


def _decode(cursor: str | None) -> dict[str, Any] | None:
    """游標字串 → dict。壞掉時 `CursorError`，由 API 層轉 422。

    **不吞掉錯誤當成第一頁**：那會讓前端的無限捲動永遠停在開頭，看起來像「載不完」。
    """
    if cursor is None:
        return None
    try:
        return decode_cursor(cursor)
    except CursorError as exc:
        # `common/` 不 import `core/`（鐵則 2），所以轉換在這一層做。不轉的話，
        # `CursorError` 是裸的 ValueError，會掉進兜底的 handler 變成 500——把
        # client 的錯誤記成我們的錯誤，而且 500 會進錯誤率告警。
        raise ValidationFailedError("游標不正確") from exc


def _conversation_view(conversation: Any) -> ConversationView:
    return ConversationView(
        id=conversation.id,
        title=conversation.title,
        kb_ids=list(conversation.kb_ids or []),
        prompt_key=conversation.prompt_key,
        status=conversation.status,
        pinned=conversation.pinned,
        message_count=conversation.message_count,
        last_message_at=conversation.last_message_at,
    )


def message_view(message: Any) -> MessageView:
    """model → DTO。**`chat.py` 也用它**：兩份對映會漂，而漂掉時 API 少一個欄位。"""
    return MessageView(
        id=message.id,
        role=message.role,
        content=message.content,
        citations=list(message.citations or []),
        model=message.model,
        status=message.status,
        usage=dict(message.usage or {}),
        created_at=message.created_at,
    )


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "ConversationService",
    "ConversationView",
    "CursorError",
    "MessageView",
    "Page",
]

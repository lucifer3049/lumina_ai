"""`/conversations` 端點（09 §2.4）。

鐵則 3（Controller 三行原則）：解析請求 → 呼叫一個 Service 方法 → 回傳。

**權限分兩層，兩層都必要**：

- `RequireScope("chat:use")` —— 角色權限：這個人能不能用聊天。
- **擁有者判定在 Service**（`ConversationService._require_own`）—— 資源權限：這場對話
  是不是他的。09 §2.4 對詳情／修改／刪除標的就是它。

只做第一層的話，同租戶的任何人都讀得到別人的對話——而 **RLS 擋不住那件事**，它是
租戶級的隔離。擁有者判定沒有第二道防線。

`POST /conversations/{id}/messages`（發送訊息 → SSE）屬 1D-4，不在本檔。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from api.dependencies.auth import Principal
from api.dependencies.permissions import RequireScope
from api.schemas.conversation import (
    ConversationCreateIn,
    ConversationListOut,
    ConversationOut,
    ConversationUpdateIn,
    MessageListOut,
    MessageOut,
)
from api.schemas.problem import ERROR_RESPONSES
from core.db import run_orm
from services.conversation.conversations import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ConversationService,
    ConversationView,
    MessageView,
)

router = APIRouter(tags=["conversations"], responses=ERROR_RESPONSES)
_conversations = ConversationService()

_ChatUser = Annotated[Principal, Depends(RequireScope("chat:use"))]
_Cursor = Annotated[str | None, Query(description="上一頁回傳的 next_cursor（不透明）")]
_Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]


def _conversation_out(view: ConversationView) -> ConversationOut:
    return ConversationOut(
        id=view.id,
        title=view.title,
        kb_ids=view.kb_ids,
        prompt_key=view.prompt_key,
        status=view.status,
        pinned=view.pinned,
        message_count=view.message_count,
        last_message_at=view.last_message_at,
    )


def _message_out(view: MessageView) -> MessageOut:
    return MessageOut(
        id=view.id,
        role=view.role,
        content=view.content,
        citations=view.citations,
        model=view.model,
        status=view.status,
        usage=view.usage,
        created_at=view.created_at,
    )


@router.get("/conversations", operation_id="conversations_list")
async def list_conversations(
    principal: _ChatUser,
    cursor: _Cursor = None,
    limit: _Limit = DEFAULT_PAGE_SIZE,
) -> ConversationListOut:
    """**只列自己的**（擁有者制）——`principal.user_id` 由 Service 當成硬條件。"""
    page = await run_orm(
        _conversations.list_for_user,
        principal.tenant_id,
        principal.user_id,
        limit=limit,
        cursor=cursor,
    )
    return ConversationListOut(
        items=[_conversation_out(view) for view in page.items], next_cursor=page.next_cursor
    )


@router.post(
    "/conversations", status_code=status.HTTP_201_CREATED, operation_id="conversations_create"
)
async def create_conversation(
    body: ConversationCreateIn,
    principal: _ChatUser,
) -> ConversationOut:
    view = await run_orm(
        _conversations.create,
        principal.tenant_id,
        principal.user_id,
        title=body.title,
        kb_ids=body.kb_ids,
        prompt_key=body.prompt_key,
    )
    return _conversation_out(view)


@router.get("/conversations/{conversation_id}", operation_id="conversations_get")
async def get_conversation(
    conversation_id: uuid.UUID,
    principal: _ChatUser,
) -> ConversationOut:
    view = await run_orm(
        _conversations.get, principal.tenant_id, principal.user_id, conversation_id
    )
    return _conversation_out(view)


@router.patch("/conversations/{conversation_id}", operation_id="conversations_update")
async def update_conversation(
    conversation_id: uuid.UUID,
    body: ConversationUpdateIn,
    principal: _ChatUser,
) -> ConversationOut:
    view = await run_orm(
        _conversations.update,
        principal.tenant_id,
        principal.user_id,
        conversation_id,
        title=body.title,
        pinned=body.pinned,
        status=body.status,
    )
    return _conversation_out(view)


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="conversations_delete",
)
async def delete_conversation(
    conversation_id: uuid.UUID,
    principal: _ChatUser,
) -> None:
    await run_orm(_conversations.delete, principal.tenant_id, principal.user_id, conversation_id)


@router.get("/conversations/{conversation_id}/messages", operation_id="conversations_list_messages")
async def list_messages(
    conversation_id: uuid.UUID,
    principal: _ChatUser,
    cursor: _Cursor = None,
    limit: _Limit = DEFAULT_PAGE_SIZE,
) -> MessageListOut:
    """一頁訊息，**時間正序**（1D-5 用同一條路徑組 context，倒序會改變語意）。"""
    page = await run_orm(
        _conversations.list_messages,
        principal.tenant_id,
        principal.user_id,
        conversation_id,
        limit=limit,
        cursor=cursor,
    )
    return MessageListOut(
        items=[_message_out(view) for view in page.items], next_cursor=page.next_cursor
    )

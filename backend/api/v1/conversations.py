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
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, status
from fastapi.responses import StreamingResponse

from api.background import ensure_capacity, spawn
from api.dependencies.auth import Principal
from api.dependencies.permissions import RequireScope
from api.schemas.conversation import (
    ConversationCreateIn,
    ConversationListOut,
    ConversationOut,
    ConversationUpdateIn,
    MessageCreateIn,
    MessageListOut,
    MessageOut,
    TurnStartedOut,
)
from api.schemas.problem import ERROR_RESPONSES
from api.sse import HEARTBEAT_SECONDS, format_event, format_heartbeat
from core.db import run_orm
from core.exceptions import ErrorCode, ResumeExpiredError, ValidationFailedError
from core.streams import StreamBuffer, StreamEvent
from services.conversation.chat import ChatService
from services.conversation.conversations import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ConversationService,
    ConversationView,
    MessageView,
)

router = APIRouter(tags=["conversations"], responses=ERROR_RESPONSES)
_conversations = ConversationService()
_chat = ChatService()

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


@router.post(
    "/conversations/{conversation_id}/messages",
    status_code=status.HTTP_201_CREATED,
    operation_id="conversations_send_message",
)
async def send_message(
    conversation_id: uuid.UUID,
    body: MessageCreateIn,
    principal: _ChatUser,
    response: Response,
    stream: Annotated[bool, Query(description="false = 等生成完成再回完整訊息")] = True,
) -> TurnStartedOut | MessageOut:
    """送出一個問題（09 §2.4）。**建立回合就回，生成跑在背景。**

    串流本身在 `GET .../stream`。拆成兩步的理由見 `services/conversation/chat.py`
    ——一句話版本：這個 POST 若同時回串流，網路閃斷時 client 分不出單子送出去了沒，
    重送一次就是兩則訊息、兩次生成、兩次帳單。
    """
    if stream:
        # 行程級的過載保護（二次架構審計 F-04）。**擋在建立回合之前**：擋在後面的話，
        # 被拒的請求已經寫了兩則訊息、扣了三種額度，而使用者只拿到一個 429。
        # 非串流那條路不經這裡——它是同步等待，併發由 uvicorn 自己的 worker 數擋著，
        # 而它本來就標為技術債（09 §5）。
        ensure_capacity()

    turn = await run_orm(
        _chat.start_turn,
        principal.tenant_id,
        principal.user_id,
        conversation_id,
        content=body.content,
    )

    if not stream:
        # 非串流模式：同一條 `generate()`，只是等它跑完（09 §5 已標為技術債）。
        # **狀態碼要改回 200**：201 的語意是「建立了一個之後要去取的資源」，而這條
        # 路徑回的就是最終結果本身，沒有第二步可去。
        response.status_code = status.HTTP_200_OK
        await _chat.generate(principal.tenant_id, turn)
        message = await _chat.wait_for_result(principal.tenant_id, turn.message_id)
        return _message_out(message)

    spawn(_chat.generate(principal.tenant_id, turn))
    return TurnStartedOut(
        message_id=turn.message_id,
        user_message_id=turn.user_message_id,
        conversation_id=conversation_id,
        stream_url=f"/api/v1/conversations/{conversation_id}/messages/{turn.message_id}/stream",
    )


@router.get(
    "/conversations/{conversation_id}/messages/{message_id}/stream",
    operation_id="conversations_stream_message",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "SSE 事件串流"}},
)
async def stream_message(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    principal: _ChatUser,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """讀一則訊息的生成串流（09 §3.2）。**帶 `Last-Event-ID` 就是續傳。**

    **這是第二個入口，而它最容易漏掉擁有者判定**：RLS 只擋租戶，擋不了同租戶的另一個
    使用者（1D-2 已經踩過這條），而 `message_id` 會出現在前端的網址與 log 裡。
    """
    after = _parse_last_event_id(last_event_id)
    message_status = await run_orm(
        _chat.require_readable, principal.tenant_id, principal.user_id, conversation_id, message_id
    )

    buffer = StreamBuffer(tenant_id=principal.tenant_id, message_id=message_id)
    if not await buffer.exists() and (after > 0 or message_status != "streaming"):
        # 緩衝區過期（TTL 5 分鐘）。**還在生成時不算**——那只是第一個事件還沒寫進來，
        # 而讀取端本來就可能比產生端早到。
        raise ResumeExpiredError()

    return StreamingResponse(
        _sse(principal.tenant_id, message_id, after=after),
        media_type="text/event-stream",
        headers={
            # proxy 不得緩衝：緩衝之後整段回答會在結束時一次送達，逐字顯示消失，
            # 而那看起來像「模型很慢」。
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _parse_last_event_id(raw: str | None) -> int:
    """`Last-Event-ID` → 起始編號。沒帶就是 0（從頭）。

    壞掉的值回 **422 而不是當成 0**（同 1D-2 對游標的決定）：當成 0 的話，一個編號寫錯
    的 client 會每次重連都從頭收一遍，而畫面上是「回答一直重複」——沒有錯誤，只有怪
    現象。
    """
    if raw is None:
        return 0
    if not raw.isdigit():
        raise ValidationFailedError(f"Last-Event-ID 必須是事件編號：{raw!r}")
    return int(raw)


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/stop",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="conversations_stop_message",
)
async def stop_message(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    principal: _ChatUser,
) -> None:
    """中止生成（09 §2.4）。

    **202 而不是 204**：真正停下來的是**另一個行程**裡的那個 task（11 §45：每 replica
    兩個 worker × N replica），這裡只是把旗標放上去。09 §3.3 的 202 慣例講的就是這種
    「已受理、還沒發生」。

    已經結束的生成照樣回 202：使用者在最後一個 token 到達的同一瞬間按下停止，那是他
    躲不掉的競態，不該因此看到錯誤。
    """
    await run_orm(
        _chat.require_readable, principal.tenant_id, principal.user_id, conversation_id, message_id
    )
    await StreamBuffer(tenant_id=principal.tenant_id, message_id=message_id).request_stop()


async def _sse(
    tenant_id: uuid.UUID, message_id: uuid.UUID, *, after: int = 0
) -> AsyncGenerator[str, None]:
    """緩衝區 → 線上的位元組。

    **`after` 之後開始**：沒帶 `Last-Event-ID` 時是 0，也就是從頭。生成在 POST 當下就
    開始了，client 可能過一會兒才接上（慢網路、頁面還在載入）——從「現在」開始讀的話，
    回答會從中間開始。
    """
    buffer = StreamBuffer(tenant_id=tenant_id, message_id=message_id)
    last_seq = after
    misses = 0
    while True:
        # `int()`：Redis 的 XREAD BLOCK 只吃整數毫秒（浮點會被退回
        # `block must be a non-negative integer`），而測試會把間隔調成 0.05 秒。
        events = await buffer.follow(after=last_seq, block_ms=int(HEARTBEAT_SECONDS * 1000))
        if not events:
            if await buffer.exists():
                misses = 0
            else:
                misses += 1
            if misses >= _MISSES_BEFORE_GIVING_UP:
                # 緩衝區不在了，而且不是「還沒開始寫」（見 `_MISSES_BEFORE_GIVING_UP`）。
                # 產生它的行程已經不在了——硬崩潰（OOM、kill -9）沒有機會寫終局事件，
                # 而緩衝區 5 分鐘後就過期。沒有這個判斷的話，這條迴圈會**永遠**送心跳：
                # 前端一直顯示「正在輸入」、一條連線一直掛著，而 DB 那一列早就被補償
                # 掃描標成 interrupted 了（它改得動 DB，改不動一個已經不存在的緩衝區）。
                yield format_event(_interrupted_event(last_seq))
                return
            # 等待逾時：送一個心跳讓中間的 proxy 看到流量（09 §3.2）。
            yield format_heartbeat()
            continue
        misses = 0
        for event in events:
            last_seq = event.seq
            yield format_event(event)
            if event.type in _TERMINAL_EVENTS:
                return


# 收到這兩種就結束——`done` 是正常講完，`error` 是中斷（1D-3a 的分水嶺之後）。
# 少了這個判斷，讀取端會在生成結束後繼續空轉送心跳，直到 client 自己關掉為止。
_TERMINAL_EVENTS = frozenset({"done", "error"})

# 連續幾次「等不到事件、而且緩衝區也不在」才判定生成已死。
#
# **不能只看一次**：讀取端本來就可能比產生端早到（POST 回來之後 client 立刻連上，而
# 第一個事件還沒寫進去），那一瞬間緩衝區確實不存在。連線當下的那一關已經處理過這件事
# （`stream_message` 對 `after > 0` 或狀態非 streaming 的組合回 409），這裡處理的是
# 「連上之後才死掉」，所以取兩次——一次心跳的間隔內出現又消失是不可能的。
_MISSES_BEFORE_GIVING_UP = 2


def _interrupted_event(seq: int) -> StreamEvent:
    """緩衝區消失時補一個終局事件，形狀與 `ChatService._fail` 送的那個相同。

    **`seq` 沿用最後一個真的事件的編號**：這個事件不是緩衝區裡的一列，給它一個更大的
    號碼會讓 client 之後拿著它去續傳，而那個編號指向不存在的東西。
    """
    return StreamEvent(
        seq=seq,
        type="error",
        data={
            "code": str(ErrorCode.STREAM_INTERRUPTED),
            "title": "生成已中斷，請重新載入這則訊息",
            "retryable": True,
        },
    )

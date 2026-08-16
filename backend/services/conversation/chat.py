"""ChatService —— 一次問答的編排（09 §2.4、06 §3、1D-4a）。

**Phase 1 的價值迴路在這裡接起來**：1D-2 建好對話、1D-3b 說出要遵守什麼規則、1D-3a
負責講話，而這一層把它們串成「使用者按下送出之後會發生的事」。

編排拆成兩段，而**這個拆法就是 1D-4a 的主要決定**：

    start_turn()  同步、在請求裡跑完：驗擁有者 → 存 user 訊息 → 建 assistant 訊息
    generate()    非同步、跑在背景：呼叫 LLM → 逐段寫進緩衝區 → 收尾持久化

兩段之間只靠 `message_id` 相連。好處是三件事同時成立：

1. **client 斷線不影響生成**（06 §4 的 G-06）。生成不掛在那條 HTTP 連線上，所以斷線
   只是沒有人在讀——而 token 的錢在斷線那一刻已經花掉了，把它收完並存好才是對的。
2. **重送不會變成兩則訊息**。第一段是普通的 JSON 請求，冪等鍵（09 §1.1）掛得上去；
   若把建立與串流塞進同一個 POST，網路閃斷時 client 分不出單子送出去了沒。
3. **驗證與授權是普通的 HTTP 錯誤**。空問題是 422、不是他的對話是 404，不必包成
   串流事件再讓 client 去解。

**失敗一律走完整條收尾路徑**（`_finish`），不是往上拋：那時已經沒有請求在等了，例外
只會落進背景 task 的黑洞，而使用者看到的是一則永遠停在「正在輸入」的訊息。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ai.gateway import AIGateway, build_gateway
from ai.gateway.chat import (
    ChatMessage,
    ChatRequest,
    DoneDelta,
    ErrorDelta,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
)
from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from core.db import run_orm
from core.exceptions import DomainError, ErrorCode, NotFoundError, ProviderError
from core.streams import StreamBuffer
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.conversation import ConversationRepository, MessageRepository
from services.ai.prompts import SYSTEM_RAG_PROMPT_KEY, PromptService
from services.conversation.conversations import MessageView, message_view

logger = get_logger(__name__)

__all__ = ["ChatService", "TurnStarted"]

# 06 §5 的記憶視窗：近 10 輪。一問一答算兩則，所以取 20 則。
#
# 摘要壓縮（超出視窗的輪次併進 summary）屬 Phase 3C——在那之前，一場很長的對話會
# 直接失去更早的內容。這是**刻意的**：沒有摘要時的正確行為是「記得最近的」，而不是
# 「把全部塞進 context 直到爆掉」。
HISTORY_WINDOW_MESSAGES = 20


@dataclass(frozen=True, slots=True)
class TurnStarted:
    """第一段的產出。`message_id` 是第二段（串流、停止、重連）唯一的定位鍵。"""

    message_id: uuid.UUID
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    model: str


class ChatService:
    def __init__(
        self,
        *,
        conversations: ConversationRepository | None = None,
        messages: MessageRepository | None = None,
        prompts: PromptService | None = None,
        gateway: AIGateway | None = None,
    ) -> None:
        self._conversations = conversations or ConversationRepository()
        self._messages = messages or MessageRepository()
        self._prompts = prompts or PromptService()
        # Gateway 惰性建立，理由同 `RetrievalService`：`build_gateway()` 會解析 provider
        # 名稱，而缺金鑰時直接 raise——建構 service 本身不該因此失敗。
        self._gateway = gateway

    @property
    def gateway(self) -> AIGateway:
        if self._gateway is None:
            self._gateway = build_gateway()
        return self._gateway

    # ── 第一段：建立回合（同步，跑在請求裡）────────────────────────

    def start_turn(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        content: str,
    ) -> TurnStarted:
        """驗擁有者、存問題、建一則 `streaming` 的回答。

        **user 訊息與 assistant 訊息在同一個交易裡建立**：分開的話，中間失敗會留下
        一個「有問題、沒有回答的位置」的狀態，而前端看不出那一輪是不是還在跑。
        """
        question = content.strip()
        if not question:
            # 空問題不該花一次 LLM 呼叫的錢，而且它一定是 client 的 bug。
            raise EmptyMessageError()

        with tenant_context(tenant_id), unit_of_work():
            conversation = self._conversations.get_by_id(conversation_id)
            if conversation is None or conversation.user_id != user_id:
                # 兩種失敗合併成 404（09 §2.3、1D-2 的擁有者制）：403 等於承認那個 id
                # 存在，而 RLS 只擋租戶、擋不了同租戶的另一個使用者。
                raise NotFoundError("對話不存在")

            user_message = self._messages.append(
                conversation_id=conversation_id, role="user", content=question
            )
            answer = self._messages.append(
                conversation_id=conversation_id,
                role="assistant",
                content="",
                status="streaming",
            )

        return TurnStarted(
            message_id=uuid.UUID(str(answer.id)),
            conversation_id=conversation_id,
            user_message_id=uuid.UUID(str(user_message.id)),
            model=get_app_settings().ai_chat_model,
        )

    # ── 第二段：生成（非同步，跑在背景）──────────────────────────

    async def generate(self, tenant_id: uuid.UUID, turn: TurnStarted) -> None:
        """跑完一次生成，把每一段寫進緩衝區，最後收尾持久化。

        **不往上拋任何例外**：這個函式跑在背景 task 裡，沒有人接得住——而使用者那邊
        看到的會是一則永遠停在「正在輸入」的訊息。所有失敗都轉成 `error` 事件加上一次
        狀態收尾。
        """
        buffer = StreamBuffer(tenant_id=tenant_id, message_id=turn.message_id)
        text: list[str] = []
        usage: dict[str, Any] = {}
        model = turn.model
        prompt_version: int | None = None

        try:
            request, prompt_version = await self._build_request(tenant_id, turn)
            await buffer.append(
                "meta",
                {
                    "message_id": str(turn.message_id),
                    "model": model,
                    "conversation_id": str(turn.conversation_id),
                },
            )

            async for delta in self.gateway.stream_chat(request):
                if isinstance(delta, TextDelta):
                    text.append(delta.text)
                    await buffer.append("delta", {"text": delta.text})
                elif isinstance(delta, UsageDelta):
                    model = delta.model or model
                    usage = {
                        "prompt_tokens": delta.prompt_tokens,
                        "completion_tokens": delta.billable_output_tokens,
                        # 單價表屬 2A（05 §3.3 的 model_configs）。填 0 會讓成本統計
                        # 把這次呼叫當成免費，所以留 None：「還不知道」與「不用錢」
                        # 是兩件事。
                        "cost": None,
                    }
                    await buffer.append("usage", dict(usage))
                elif isinstance(delta, ToolCallDelta):
                    # 3A 才有真的工具；型別現在就對得上，事件因此不必等那時才加。
                    await buffer.append(
                        "tool_call",
                        {
                            "name": delta.name,
                            "params_preview": delta.arguments,
                            "status": delta.status,
                        },
                    )
                elif isinstance(delta, ErrorDelta):
                    # 分水嶺之後的中斷（1D-3a）：已交付的內容留著。
                    await self._fail(
                        buffer,
                        turn,
                        tenant_id,
                        code=delta.code,
                        message=delta.message,
                        retryable=delta.retryable,
                        status="interrupted",
                        text=text,
                        usage=usage,
                        model=model,
                        prompt_version=prompt_version,
                    )
                    return
                elif isinstance(delta, DoneDelta):
                    await self._complete(
                        buffer,
                        turn,
                        tenant_id,
                        finish_reason=delta.finish_reason,
                        text=text,
                        usage=usage,
                        model=model,
                        prompt_version=prompt_version,
                    )
                    return
        except ProviderError as exc:
            # 第一個 token 之前就失敗（1D-3a 的分水嶺之前是例外）。一個字都沒產生，
            # 所以重試是乾淨的——code 與中斷那一種必須分得開。
            await self._fail(
                buffer,
                turn,
                tenant_id,
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
                status="failed",
                text=text,
                usage=usage,
                model=model,
                prompt_version=prompt_version,
            )
        except Exception as exc:
            logger.exception("chat_generation_crashed", message_id=str(turn.message_id))
            await self._fail(
                buffer,
                turn,
                tenant_id,
                code=ErrorCode.INTERNAL_ERROR,
                # 內部錯誤的訊息不落地（鐵則 9）：它會經 SSE 直接到租戶眼前。
                message="生成時發生非預期的錯誤",
                retryable=True,
                status="failed",
                text=text,
                usage=usage,
                model=model,
                prompt_version=prompt_version,
                cause=type(exc).__name__,
            )

    def require_readable(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
    ) -> None:
        """串流端點的守門：這則訊息在他自己的對話裡嗎？

        **拆成兩步之後多了一個入口，而這個入口最容易漏掉判定。** RLS 只擋租戶，擋不了
        同租戶的另一個使用者（1D-2 已經踩過），而 `message_id` 會出現在前端的網址與
        log 裡——漏掉的話，拿得到那個 id 的人就讀得到別人正在生成的回答。
        """
        with tenant_context(tenant_id), unit_of_work():
            conversation = self._conversations.get_by_id(conversation_id)
            if conversation is None or conversation.user_id != user_id:
                raise NotFoundError("對話不存在")
            belongs = (
                self._messages.get_queryset()
                .filter(id=message_id, conversation_id=conversation_id)
                .exists()
            )
            if not belongs:
                raise NotFoundError("訊息不存在")

    async def wait_for_result(self, tenant_id: uuid.UUID, message_id: uuid.UUID) -> MessageView:
        """非串流模式（`?stream=false`）用：生成跑完之後把訊息讀回來。

        **與串流走同一條 `generate()`**（09 §5 已把「兩種模式共用一個端點」標為技術
        債）：分成兩條的話，其中一條的持久化、計費或快照遲早會漏一項，而那條路徑的
        使用者是整合方——最不會回報問題的一群。
        """
        return await run_orm(self._read_message, tenant_id, message_id)

    def _read_message(self, tenant_id: uuid.UUID, message_id: uuid.UUID) -> MessageView:
        with tenant_context(tenant_id), unit_of_work():
            message = self._messages.get_queryset().filter(id=message_id).first()
        if message is None:  # pragma: no cover —— 剛剛才建的那一列
            raise NotFoundError("訊息不存在")
        return message_view(message)

    # ── 內部 ────────────────────────────────────────────────────

    async def _build_request(
        self, tenant_id: uuid.UUID, turn: TurnStarted
    ) -> tuple[ChatRequest, int]:
        """system prompt（1D-3b）+ 近 N 輪原文 → `ChatRequest`。

        歷史從 DB 讀而不是由呼叫端傳：那是唯一一份真相，而「前端送什麼就用什麼」等於
        讓 client 決定 LLM 看得到哪些內容——它可以偽造一段從未發生過的對話。
        """
        rendered = await run_orm(self._prompts.render, tenant_id, key=SYSTEM_RAG_PROMPT_KEY)
        history = await run_orm(self._history, tenant_id, turn)

        messages = [ChatMessage(role="system", content=rendered.system), *history]
        return ChatRequest(messages=messages, model=turn.model), rendered.version

    def _history(self, tenant_id: uuid.UUID, turn: TurnStarted) -> list[ChatMessage]:
        """近 N 輪（含這一輪的問題）。**跳過還在生成的那一則**——它的內容是空的。"""
        with tenant_context(tenant_id), unit_of_work():
            rows = self._messages.for_conversation(
                turn.conversation_id, limit=HISTORY_WINDOW_MESSAGES
            )
        return [
            ChatMessage(role=_role_of(row.role), content=row.content)
            for row in rows
            if row.content and uuid.UUID(str(row.id)) != turn.message_id
        ]

    async def _complete(
        self,
        buffer: StreamBuffer,
        turn: TurnStarted,
        tenant_id: uuid.UUID,
        *,
        finish_reason: str,
        text: Sequence[str],
        usage: dict[str, Any],
        model: str,
        prompt_version: int | None,
    ) -> None:
        await run_orm(
            self._persist,
            tenant_id,
            turn.message_id,
            status="completed",
            content="".join(text),
            usage=usage,
            model=model,
            prompt_version=prompt_version,
        )
        await buffer.append(
            "done", {"message_id": str(turn.message_id), "finish_reason": finish_reason}
        )
        logger.info(
            "chat_turn_completed",
            message_id=str(turn.message_id),
            model=model,
            prompt_version=prompt_version,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    async def _fail(
        self,
        buffer: StreamBuffer,
        turn: TurnStarted,
        tenant_id: uuid.UUID,
        *,
        code: ErrorCode,
        message: str,
        retryable: bool,
        status: str,
        text: Sequence[str],
        usage: dict[str, Any],
        model: str,
        prompt_version: int | None,
        cause: str | None = None,
    ) -> None:
        """收尾：**先持久化再送事件**。

        反過來的話，client 收到 error 之後立刻去抓最終訊息，會讀到一則還停在
        `streaming` 的列——而那是一個看起來像「還在跑」的已結束訊息。
        """
        await run_orm(
            self._persist,
            tenant_id,
            turn.message_id,
            status=status,
            content="".join(text),
            usage=usage,
            model=model,
            prompt_version=prompt_version,
            error={"code": str(code), "cause": cause} if cause else {"code": str(code)},
        )
        await buffer.append("error", {"code": str(code), "title": message, "retryable": retryable})
        logger.warning(
            "chat_turn_failed",
            message_id=str(turn.message_id),
            code=str(code),
            status=status,
            produced_characters=sum(len(part) for part in text),
        )

    def _persist(
        self,
        tenant_id: uuid.UUID,
        message_id: uuid.UUID,
        *,
        status: str,
        content: str,
        usage: dict[str, Any],
        model: str,
        prompt_version: int | None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """把生成的結果寫回那一列（05 §3.4 的生成快照）。

        `model` 與 `prompt_version` 是「這個回答當時用了什麼」的唯一紀錄——漏記的話，
        3B 的評測與事故回溯都失去依據，而那正是 06 §1 的版本化貫穿要保證的事。
        """
        with tenant_context(tenant_id), unit_of_work():
            self._messages.set_status(
                message_id,
                status=status,
                error=error,
                content=content,
                usage=usage,
                model=model,
                prompt_version=prompt_version,
            )


class EmptyMessageError(DomainError):
    """空白的問題。422（09 §1.3 的語意驗證失敗）。"""

    code = ErrorCode.VALIDATION_FAILED

    def __init__(self) -> None:
        super().__init__("訊息內容不得為空")


def _role_of(role: str) -> Any:
    """DB 的 role 字串 → `ChatMessage` 的 role。

    未知的值退成 `user`：那比讓整次生成因為一個沒見過的字串而失敗好，而 05 §3.4 的
    四個值（user/assistant/tool/system）本來就涵蓋得了現在的寫入路徑。
    """
    return role if role in {"system", "user", "assistant", "tool"} else "user"

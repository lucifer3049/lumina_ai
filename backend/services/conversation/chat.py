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

import asyncio
import contextlib
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
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
from ai.prompts import ContextChunk, build_context_block, build_user_turn
from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from core.db import run_orm
from core.exceptions import DomainError, ErrorCode, NotFoundError, ProviderError
from core.streams import StreamBuffer
from core.tenant import tenant_context
from core.uow import unit_of_work
from etl.tokens import estimate_tokens
from rag.citation import assemble_citations, marker_for
from rag.pipeline import build_search_query
from rag.retrievers.vector import RetrievedChunk
from repositories.conversation import ConversationRepository, MessageRepository
from services.ai.prompts import SYSTEM_RAG_PROMPT_KEY, PromptService
from services.conversation.conversations import MessageView, message_view
from services.platform.quota import QuotaExceededError, QuotaReservation, QuotaService
from services.platform.usage import UsageEvent, UsageService
from services.rag.retrieval import RetrievalOutcome, RetrievalService

logger = get_logger(__name__)

__all__ = ["ChatService", "TurnStarted"]

# 06 §5 的記憶視窗：近 10 輪。一問一答算兩則，所以取 20 則。
#
# 摘要壓縮（超出視窗的輪次併進 summary）屬 Phase 3C——在那之前，一場很長的對話會
# 直接失去更早的內容。這是**刻意的**：沒有摘要時的正確行為是「記得最近的」，而不是
# 「把全部塞進 context 直到爆掉」。
HISTORY_WINDOW_MESSAGES = 20

# 中止旗標的輪詢間隔（1D-4b）。**不是每個 token 問一次**：那是每個 token 一趟 Redis
# 往返，而 token 是以百計的。0.2 秒是「使用者按下停止到真的停下來」的上限，那遠低於
# 人的感知門檻，而省下來的是幾百趟往返。
STOP_POLL_SECONDS = 0.2


def _token_reserve_for(question: str) -> int:
    """開場要預留多少 token 額度（reserve/commit 的 reserve 值）。

    **設定值 + 這個問題本身的估計量**。設定值（`quota_token_reserve_estimate`）涵蓋的
    是「答案 + context」那一段——它們的長度要到生成結束才知道，只能先估。但問題本身
    的長度**現在就知道**，而它可以差好幾個量級：一則貼滿的訊息（schema 上限 32,000
    字元）光是問題就三萬多 token，用一個固定的 2000 去擋線等於沒擋——真實用量要等
    收尾 commit 才追認，那時該擋的那幾則已經送出去了。

    高估的代價只有「月底最後幾則被提早擋下」，而低估的代價是超用之後才發現。
    """
    return get_app_settings().quota_token_reserve_estimate + estimate_tokens(question)


@dataclass(frozen=True, slots=True)
class TurnStarted:
    """第一段的產出。`message_id` 是第二段（串流、停止、重連）唯一的定位鍵。

    `question` 與 `kb_ids` 是 1D-5 加的：第二段要用它們做檢索，而它們在第一段就已經
    讀出來了——再查一次等於每則訊息多一趟 DB，且中間 KB 被改掉的話兩段會不一致。
    """

    message_id: uuid.UUID
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    model: str
    question: str = ""
    kb_ids: tuple[uuid.UUID, ...] = ()
    # 2A-1：usage 落地要記「誰花的錢」，而第二段跑在背景、手上只有這個物件。
    user_id: uuid.UUID | None = None
    # 2A-2a：第一段預留的額度，第二段收尾時 commit（實際 token）／release（並發位）。
    token_reservation: QuotaReservation | None = None
    stream_reservation: QuotaReservation | None = None


@dataclass(frozen=True, slots=True)
class _PreparedTurn:
    """送出去之前組好的一切。`chunks` 留到收尾時比對引用（06 §3.3）。"""

    request: ChatRequest
    prompt_version: int
    chunks: tuple[RetrievedChunk, ...]
    retrieved: bool
    # 哪幾個增強步驟被跳過了（2B-3）——一路走到 `usage.rag.degraded`。
    degraded: tuple[str, ...] = ()


class ChatService:
    def __init__(
        self,
        *,
        conversations: ConversationRepository | None = None,
        messages: MessageRepository | None = None,
        prompts: PromptService | None = None,
        retrieval: RetrievalService | None = None,
        gateway: AIGateway | None = None,
        usage: UsageService | None = None,
        quota: QuotaService | None = None,
    ) -> None:
        self._conversations = conversations or ConversationRepository()
        self._messages = messages or MessageRepository()
        self._prompts = prompts or PromptService()
        self._retrieval = retrieval or RetrievalService()
        self._usage = usage or UsageService()
        self._quota = quota or QuotaService()
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

        # 配額擋線（2A-2a）：在**建立任何訊息之前**。擋在後面的話，429 的請求會留下
        # 「有問題、永遠沒有回答」的半個回合，而它還吃掉了一則訊息額度。
        # 任何一關被擋就把前面預留的全部歸還——被擋的請求不得消耗額度。
        reservations: list[QuotaReservation] = []
        token_reservation: QuotaReservation | None = None
        stream_reservation: QuotaReservation | None = None
        try:
            if r := self._quota.check_and_reserve(tenant_id, "messages_day", 1):
                reservations.append(r)
            token_reservation = self._quota.check_and_reserve(
                tenant_id, "tokens_month", _token_reserve_for(question)
            )
            if token_reservation:
                reservations.append(token_reservation)
            stream_reservation = self._quota.check_and_reserve(tenant_id, "streams", 1)
            if stream_reservation:
                reservations.append(stream_reservation)
        except QuotaExceededError:
            for reservation in reservations:
                self._quota.release(reservation)
            raise

        try:
            turn = self._create_turn(tenant_id, user_id, conversation_id, question=question)
        except Exception:
            # DB 那一步失敗（對話不存在、寫入錯誤）不能吃掉額度。
            for reservation in reservations:
                self._quota.release(reservation)
            raise
        return replace(
            turn, token_reservation=token_reservation, stream_reservation=stream_reservation
        )

    def _create_turn(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        question: str,
    ) -> TurnStarted:
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

            # KB 清單在交易內取出：離開之後再碰屬性會觸發新查詢，而那時沒有租戶
            # context（RLS 讀不到東西）。
            kb_ids = tuple(uuid.UUID(str(kb_id)) for kb_id in (conversation.kb_ids or []))

        return TurnStarted(
            message_id=uuid.UUID(str(answer.id)),
            conversation_id=conversation_id,
            user_message_id=uuid.UUID(str(user_message.id)),
            model=get_app_settings().ai_chat_model,
            question=question,
            kb_ids=kb_ids,
            user_id=user_id,
        )

    # ── 第二段：生成（非同步，跑在背景）──────────────────────────

    async def generate(self, tenant_id: uuid.UUID, turn: TurnStarted) -> None:
        """跑完一次生成（`_generate`），並保證並發位在**任何**路徑都被歸還。

        finally 而不是散在各收尾點：generate 的出口有完成、中止、error、例外四種，
        散寫漏掉哪一種，那一種就開始洩漏並發位——第 N 輪之後這個租戶永遠 429，
        而那看起來像「配額壞了」。
        """
        try:
            await self._generate(tenant_id, turn)
        finally:
            if turn.stream_reservation is not None:
                await run_orm(self._quota.release, turn.stream_reservation)

    async def _generate(self, tenant_id: uuid.UUID, turn: TurnStarted) -> None:
        """把每一段寫進緩衝區，最後收尾持久化。

        **不往上拋任何例外**：這個函式跑在背景 task 裡，沒有人接得住——而使用者那邊
        看到的會是一則永遠停在「正在輸入」的訊息。所有失敗都轉成 `error` 事件加上一次
        狀態收尾。
        """
        buffer = StreamBuffer(tenant_id=tenant_id, message_id=turn.message_id)
        text: list[str] = []
        usage: dict[str, Any] = {}
        model = turn.model
        prompt_version: int | None = None
        prepared: _PreparedTurn | None = None

        try:
            prepared = await self._prepare(tenant_id, turn)
            request, prompt_version = prepared.request, prepared.prompt_version
            await buffer.append(
                "meta",
                {
                    "message_id": str(turn.message_id),
                    "model": model,
                    "conversation_id": str(turn.conversation_id),
                },
            )

            stop_checked_at = time.monotonic()
            async for delta in self.gateway.stream_chat(request):
                now = time.monotonic()
                if now - stop_checked_at >= STOP_POLL_SECONDS:
                    stop_checked_at = now
                    if await buffer.stop_requested():
                        # 使用者自己按的停止**不是錯誤**：他剛剛得到的正是他要的結果。
                        # 送 error 的話前端會顯示一個紅色的失敗訊息（09 §1.3）。
                        await self._complete(
                            buffer,
                            turn,
                            tenant_id,
                            finish_reason="stopped",
                            status="interrupted",
                            text=text,
                            usage=usage,
                            model=model,
                            prompt_version=prompt_version,
                            prepared=prepared,
                        )
                        return
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
                        prepared=prepared,
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
                        prepared=prepared,
                    )
                    return
        except asyncio.CancelledError:
            # 關機時被 `drain()` 取消（11 §196）。**要留下痕跡再走**：不留的話，使用者
            # 的畫面停在半句話、而資料庫裡那一則永遠是 `streaming`——重整也不會變，
            # 因為沒有人會再去動它。
            #
            # 收尾用 `shield` 包住：這個 task 已經被要求取消，接下來的每一個 await 都
            # 可能立刻再收到 CancelledError，而那會讓收尾只做一半（事件送了、狀態沒改）。
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    self._fail(
                        buffer,
                        turn,
                        tenant_id,
                        code=ErrorCode.STREAM_INTERRUPTED,
                        message="伺服器正在重啟，生成已中斷",
                        retryable=True,
                        status="interrupted",
                        text=text,
                        usage=usage,
                        model=model,
                        prompt_version=prompt_version,
                        prepared=prepared,
                        cause="shutdown",
                    )
                )
            raise
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
                prepared=prepared,
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
                prepared=prepared,
                cause=type(exc).__name__,
            )

    async def _settle_token_quota(self, turn: TurnStarted, usage: dict[str, Any]) -> None:
        """token 額度的第二段（2A-2a）：有實際用量就 commit 校正，沒有就整筆歸還。

        不歸還的話，provider 一個字都沒吐的失敗回合也吃掉 2000 的預留量——
        連續幾次失敗之後，額度被「失敗」吃光，而 usage_logs 裡一筆帳都沒有。
        """
        if turn.token_reservation is None:
            return
        if usage:
            actual = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
            await run_orm(self._quota.commit, turn.token_reservation, actual=actual)
        else:
            await run_orm(self._quota.release, turn.token_reservation)

    def require_readable(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
    ) -> str:
        """串流端點的守門：這則訊息在他自己的對話裡嗎？回傳它的狀態。

        **回狀態是給續傳判斷用的**（1D-4b）：緩衝區不在時，「還在生成」與「早就結束」
        要走不同的路——前者是「第一個事件還沒寫進來」（正常，繼續等），後者是
        「緩衝區過期了」（409，改抓最終訊息）。

        **拆成兩步之後多了一個入口，而這個入口最容易漏掉判定。** RLS 只擋租戶，擋不了
        同租戶的另一個使用者（1D-2 已經踩過），而 `message_id` 會出現在前端的網址與
        log 裡——漏掉的話，拿得到那個 id 的人就讀得到別人正在生成的回答。
        """
        with tenant_context(tenant_id), unit_of_work():
            conversation = self._conversations.get_by_id(conversation_id)
            if conversation is None or conversation.user_id != user_id:
                raise NotFoundError("對話不存在")
            message = (
                self._messages.get_queryset()
                .filter(id=message_id, conversation_id=conversation_id)
                .first()
            )
            if message is None:
                raise NotFoundError("訊息不存在")
            return str(message.status)

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

    async def _prepare(self, tenant_id: uuid.UUID, turn: TurnStarted) -> _PreparedTurn:
        """system prompt（1D-3b）+ 近 N 輪原文 + 檢索到的 context（1D-5）→ 請求。

        歷史從 DB 讀而不是由呼叫端傳：那是唯一一份真相，而「前端送什麼就用什麼」等於
        讓 client 決定 LLM 看得到哪些內容——它可以偽造一段從未發生過的對話。

        **組裝順序是 06 §3 的 system → memory → context → query**，而 context 與問題
        都在最後那一則 `user` 訊息裡（10 §5 的指令／資料分域，見 `build_user_turn`）。
        """
        rendered = await run_orm(self._prompts.render, tenant_id, key=SYSTEM_RAG_PROMPT_KEY)
        history, previous_questions = await run_orm(self._history, tenant_id, turn)
        outcome = await self._retrieve(tenant_id, turn, previous_questions)
        chunks = outcome.chunks

        context = ""
        if chunks:
            context = build_context_block(
                [
                    ContextChunk(
                        marker=marker_for(index),
                        text=chunk.content,
                        doc_name=chunk.document_name,
                        page=chunk.page,
                        heading_path=chunk.heading_path,
                    )
                    for index, chunk in enumerate(chunks)
                ]
            )

        messages = [
            ChatMessage(role="system", content=rendered.system),
            *history,
            ChatMessage(role="user", content=build_user_turn(turn.question, context)),
        ]
        return _PreparedTurn(
            request=ChatRequest(messages=messages, model=turn.model),
            prompt_version=rendered.version,
            chunks=tuple(chunks),
            retrieved=bool(turn.kb_ids),
            degraded=outcome.degraded,
        )

    async def _retrieve(
        self, tenant_id: uuid.UUID, turn: TurnStarted, previous_questions: Sequence[str]
    ) -> RetrievalOutcome:
        """這一輪的 context（06 §3）。沒掛 KB 就整段跳過——06 §9 的純閒聊路徑不付
        RAG 成本，而沒有 KB 時檢索一定查不到任何東西。

        回的是 `RetrievalOutcome` 而不是清單（2B-3）：降級標記要跟著走到
        `usage.rag.degraded`，而把它掛在 service 的 instance 上會在併發請求之間外洩
        ——這個 service 是跨請求共用的。
        """
        if not turn.kb_ids:
            return RetrievalOutcome(chunks=[])

        params = await run_orm(self._retrieval.params_for, tenant_id, turn.kb_ids)
        query = build_search_query(
            turn.question,
            previous_questions=previous_questions,
            history_turns=params.query_history_turns,
        )
        return await run_orm(
            self._retrieval.retrieve_for_chat, tenant_id, kb_ids=turn.kb_ids, query=query
        )

    def _history(
        self, tenant_id: uuid.UUID, turn: TurnStarted
    ) -> tuple[list[ChatMessage], list[str]]:
        """近 N 輪 → (要送給模型的歷史, 先前的問題)。

        **這一輪的問題不在歷史裡**：它與 context 一起組成最後那則 user 訊息
        （`_prepare`），留在歷史裡會讓同一個問題出現兩次。還在生成的那一則也跳過
        ——它的內容是空的。

        先前的問題另外回傳，給檢索用（`build_search_query`）：「那病假呢？」單獨拿去
        搜命中的是一組無關內容，而模型那邊本來就看得到歷史，不需要改寫過的問句。
        """
        with tenant_context(tenant_id), unit_of_work():
            rows = self._messages.for_conversation(
                turn.conversation_id, limit=HISTORY_WINDOW_MESSAGES
            )

        skip = {turn.message_id, turn.user_message_id}
        kept = [row for row in rows if row.content and uuid.UUID(str(row.id)) not in skip]
        return (
            [ChatMessage(role=_role_of(row.role), content=row.content) for row in kept],
            [str(row.content) for row in kept if row.role == "user"],
        )

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
        prepared: _PreparedTurn | None,
        status: str = "completed",
    ) -> None:
        """正常收尾。`status` 只有中止時不是 `completed`——那時 `finish_reason` 是
        `stopped`，讓前端分得出「講完了」與「被停下來」（05 §3.4 的 `interrupted`）。"""
        answer = "".join(text)
        citations, rag_stats = self._citations(answer, prepared, message_id=turn.message_id)
        await run_orm(
            self._persist,
            tenant_id,
            turn.message_id,
            status=status,
            content=answer,
            usage=_with_rag_stats(usage, rag_stats),
            model=model,
            prompt_version=prompt_version,
            citations=citations,
        )
        # usage_logs 落地（2A-1）。在 `done` **之前**：done 是前端停止讀取的訊號，也是
        # 測試查帳的時點，排在它後面的話「串流結束了、帳還沒到」是常態而不是異常。
        # record 不往外拋（旁路原則，services/platform/usage.py），且自帶交易——
        # 不會污染 _persist 已經收尾的寫入。中止的回合 usage 可能是空的（provider
        # 還沒回報就停了），那時沒有數字可記，跳過而不是記一列 0。
        if usage:
            await run_orm(
                self._usage.record,
                tenant_id,
                UsageEvent(
                    category="llm",
                    model=model,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    request_id=str(turn.message_id),
                    user_id=turn.user_id,
                    conversation_id=turn.conversation_id,
                ),
            )
        await self._settle_token_quota(turn, usage)
        await self._emit_citations(buffer, prepared, citations)
        await buffer.append(
            "done", {"message_id": str(turn.message_id), "finish_reason": finish_reason}
        )
        logger.info(
            "chat_turn_completed",
            message_id=str(turn.message_id),
            finish_reason=finish_reason,
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
        prepared: _PreparedTurn | None = None,
        cause: str | None = None,
    ) -> None:
        """收尾：**先持久化再送事件**。

        反過來的話，client 收到 error 之後立刻去抓最終訊息，會讀到一則還停在
        `streaming` 的列——而那是一個看起來像「還在跑」的已結束訊息。

        **中斷的回答也要組引用**：已經產生的那半句話裡的標記與完整回答裡的沒有差別，
        而使用者留在畫面上的就是那半句——沒有引用的話，它看起來像一段沒有依據的話。
        """
        answer = "".join(text)
        citations, rag_stats = self._citations(answer, prepared, message_id=turn.message_id)
        await run_orm(
            self._persist,
            tenant_id,
            turn.message_id,
            status=status,
            content=answer,
            usage=_with_rag_stats(usage, rag_stats),
            model=model,
            prompt_version=prompt_version,
            citations=citations,
            error={"code": str(code), "cause": cause} if cause else {"code": str(code)},
        )
        await self._settle_token_quota(turn, usage)
        await self._emit_citations(buffer, prepared, citations)
        await buffer.append("error", {"code": str(code), "title": message, "retryable": retryable})
        logger.warning(
            "chat_turn_failed",
            message_id=str(turn.message_id),
            code=str(code),
            status=status,
            produced_characters=sum(len(part) for part in text),
        )

    def _citations(
        self, answer: str, prepared: _PreparedTurn | None, *, message_id: uuid.UUID
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """回答 + 本輪 context → 驗證過的引用，以及**這一輪的三個數字**（06 §3.3）。

        `prepared` 是 `None` 代表在組請求之前就失敗了（prompt 讀不到、檢索炸了）——
        那時一個字都還沒產生，自然沒有引用可組。

        **三個數字要落地，不能只進 log**（2026-08-17 決定）：它們是 06 §3.3 的幻覺
        指標，也是 13 §3.5 第 1 項（改用短編號）唯一的量測——真 provider 接上之後，
        「模型抄不抄得對」只能從這裡看出來。留在 log 裡等於沒人會看：要回答「這個月
        有多少 % 的回答出現假引用」得去翻幾百萬行日誌，而那件事沒有人會去做第二次。
        落在 `messages.usage` 裡則是一句 SQL，且**3B 的評測不必回頭補歷史**——歷史
        補不回來。
        """
        if prepared is None or not answer:
            return [], None

        result = assemble_citations(answer, prepared.chunks)
        if result.dropped_markers:
            logger.warning(
                "citation_markers_dropped",
                message_id=str(message_id),
                dropped=result.dropped_markers,
                context_count=len(prepared.chunks),
            )
        if not prepared.retrieved:
            # 純閒聊路徑（06 §9）沒有檢索，也就沒有東西可統計。**記一組全是 0 的數字
            # 會汙染分母**——「有多少 % 的回答出現假引用」會把從來沒查過知識庫的那些
            # 也算進去，而那個比例只會愈看愈好。
            return [], None
        stats = {
            "context_chunks": len(prepared.chunks),
            "citations": len(result.citations),
            "dropped": len(result.dropped_markers),
            # 哪幾個增強步驟被跳過了（2B-3）。**正常路徑是空清單而不是省略欄位**：
            # 省略的話，「這一輪沒有降級」與「這個版本還沒有這個欄位」在報表上分不出來。
            "degraded": list(prepared.degraded),
        }
        return [citation.as_dict() for citation in result.citations], stats

    async def _emit_citations(
        self,
        buffer: StreamBuffer,
        prepared: _PreparedTurn | None,
        citations: list[dict[str, Any]],
    ) -> None:
        """`citations` 事件（09 §3.2）。**一定在 `done`／`error` 之前**——那兩個是
        client 停止讀取的訊號，排在它們後面的事件永遠不會被收到。

        **檢索跑過就送，即使是空的**：不送的話前端分不出「這是純閒聊」與「查了但
        沒有依據」，而後者要顯示的是「本回答未引用知識庫內容」——那是一個提醒，
        不是一個空白。

        `{"items": [...]}` 而不是 09 §3.2 寫的裸陣列：緩衝區的事件 data 是物件，
        而其餘六種事件也全是物件（偏離記於 13 §3.5）。
        """
        if prepared is None or not prepared.retrieved:
            return
        await buffer.append("citations", {"items": citations})

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
        citations: list[dict[str, Any]] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """把生成的結果寫回那一列（05 §3.4 的生成快照）。

        `model` 與 `prompt_version` 是「這個回答當時用了什麼」的唯一紀錄——漏記的話，
        3B 的評測與事故回溯都失去依據，而那正是 06 §1 的版本化貫穿要保證的事。

        `citations` 一起寫：只送事件不落地的話，引用會在關掉分頁的那一刻消失，而回答
        還在——那個答案看起來從一開始就沒有依據。
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
                citations=citations or [],
            )


class EmptyMessageError(DomainError):
    """空白的問題。422（09 §1.3 的語意驗證失敗）。"""

    code = ErrorCode.VALIDATION_FAILED

    def __init__(self) -> None:
        super().__init__("訊息內容不得為空")


def _with_rag_stats(usage: dict[str, Any], stats: dict[str, Any] | None) -> dict[str, Any]:
    """把這一輪的引用統計併進生成快照（05 §3.4 的 `usage jb`）。

    **放在 `usage["rag"]` 這個子物件裡，不與 token 平放**：`prompt_tokens` 那幾個鍵是
    2A 計費的原料，混在一起遲早會有人把 `dropped` 當成一種 token。分開之後查詢仍然
    只有一句（`usage->'rag'->>'dropped'`）。

    **不開新欄位**是為了不動 migration：`messages` 是分區表，加欄位要走 05 §5.2 的
    三步走，而這三個數字的價值不足以換那個成本。真的需要索引時（3B 的評測報表）再談。
    """
    if stats is None:
        return usage
    return {**usage, "rag": stats}


def _role_of(role: str) -> Any:
    """DB 的 role 字串 → `ChatMessage` 的 role。

    未知的值退成 `user`：那比讓整次生成因為一個沒見過的字串而失敗好，而 05 §3.4 的
    四個值（user/assistant/tool/system）本來就涵蓋得了現在的寫入路徑。
    """
    return role if role in {"system", "user", "assistant", "tool"} else "user"

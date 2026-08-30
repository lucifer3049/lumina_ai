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
from etl.tokens import estimate_tokens
from rag.citation import assemble_citations
from rag.trace import emit as emit_trace
from repositories.conversation import ConversationRepository, MessageRepository
from services.ai.prompts import PromptService
from services.conversation.budget import TurnBudget
from services.conversation.composer import PreparedTurn, TurnComposer
from services.conversation.conversations import MessageView, message_view
from services.platform.quota import QuotaReservation, QuotaService
from services.platform.usage import UsageEvent, UsageService
from services.rag.retrieval import RetrievalService

logger = get_logger(__name__)

__all__ = ["ChatService", "TurnStarted"]

# 中止旗標的輪詢間隔（1D-4b）。**不是每個 token 問一次**：那是每個 token 一趟 Redis
# 往返，而 token 是以百計的。0.2 秒是「使用者按下停止到真的停下來」的上限，那遠低於
# 人的感知門檻，而省下來的是幾百趟往返。
STOP_POLL_SECONDS = 0.2


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
        budget: TurnBudget | None = None,
        composer: TurnComposer | None = None,
    ) -> None:
        self._conversations = conversations or ConversationRepository()
        self._messages = messages or MessageRepository()
        self._usage = usage or UsageService()
        # 額度與組請求各自切出去了（二次架構審計 F-07）。**個別協作者的注入口留著**：
        # 既有測試注入的是 `prompts` / `retrieval` / `quota`，而 F-07 是重構不是改
        # 介面——把注入口換掉會讓「這次改動有沒有改變行為」變得無從判斷。
        self._budget = budget or TurnBudget(quota=quota)
        self._composer = composer or TurnComposer(
            prompts=prompts, retrieval=retrieval, messages=self._messages
        )
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
        # 被擋時的自我清理在 `TurnBudget.reserve` 裡（見該處）。
        reserved = self._budget.reserve(tenant_id, question=question)

        try:
            turn = self._create_turn(tenant_id, user_id, conversation_id, question=question)
        except Exception:
            # DB 那一步失敗（對話不存在、寫入錯誤）不能吃掉額度。
            self._budget.release(reserved)
            raise
        return replace(turn, token_reservation=reserved.tokens, stream_reservation=reserved.stream)

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
            await self._budget.release_stream(turn.stream_reservation)

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
        prepared: PreparedTurn | None = None

        try:
            prepared = await self._composer.compose(
                tenant_id,
                conversation_id=turn.conversation_id,
                message_id=turn.message_id,
                user_message_id=turn.user_message_id,
                question=turn.question,
                kb_ids=turn.kb_ids,
                model=turn.model,
            )
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
            # `aclosing` 而不是裸的 `async for`：stop／error／done 都以 return 提前
            # 離開迴圈，不包的話被棄置的 generator（連同 provider 的 HTTP 連線）要等
            # GC 才關——gateway 契約明講這條鏈上的每一層都要包（ai/gateway 同款）。
            # 對地端 provider 而言，晾著的那段時間 GPU 還在替沒有人要的回答產 token。
            async with contextlib.aclosing(self.gateway.stream_chat(request)) as deltas:
                async for delta in deltas:
                    now = time.monotonic()
                    if now - stop_checked_at >= STOP_POLL_SECONDS:
                        stop_checked_at = now
                        if await buffer.stop_requested():
                            # **先關上游再收尾**：收尾要做 DB 與多趟 Redis 往返，晚關
                            # 的每一毫秒 provider 都還在計費／佔 GPU。
                            await deltas.aclose()
                            # 使用者自己按的停止**不是錯誤**：他剛剛得到的正是他要的
                            # 結果。送 error 的話前端會顯示一個紅色的失敗訊息（09
                            # §1.3）。usage 此時必然還沒到（provider 在 done 前才送），
                            # 帳改用估算補——token 已經產生費用，與有沒有講完無關。
                            await self._complete(
                                buffer,
                                turn,
                                tenant_id,
                                finish_reason="stopped",
                                status="interrupted",
                                text=text,
                                usage=usage or self._estimated_usage(prepared, text),
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
                            # 單價表屬 2A（05 §3.3 的 model_configs）。填 0 會讓成本
                            # 統計把這次呼叫當成免費，所以留 None：「還不知道」與
                            # 「不用錢」是兩件事。
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
                        # 分水嶺之後的中斷（1D-3a）：已交付的內容留著。usage 不必補估
                        # ——gateway 的 `interrupted()` 在 error 前一定先補一筆。
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

    def _estimated_usage(
        self, prepared: PreparedTurn | None, text: Sequence[str]
    ) -> dict[str, Any]:
        """provider 還沒回報 usage 就結束時的估算——目前只有 stop 這一條路。

        gateway 對「講到一半斷線」已經會補估算（`_StreamState.estimated_usage`），
        唯獨 stop 是**我們主動棄讀**：generator 被關掉，補的那一筆永遠到不了。不估的
        話這一輪 usage_logs 沒有帳、token 預留整筆退回，而 provider 按產出計價（地端
        則是 GPU 真的燒了那段時間）——反覆「長問題＋快講完時 stop」就能繞過月度配額。

        用 `estimate_tokens`（與預留同一把尺，CJK 感知）而不是 gateway 的 chars//4：
        commit 校正的是這裡預留的量，兩邊用同一種估法，帳才對得攏。

        一個字都沒產生就停（text 空）不估：prompt 那一點量不值得記一筆估算帳，
        維持整筆退回的原行為。
        """
        if prepared is None or not any(text):
            return {}
        return {
            "prompt_tokens": sum(
                estimate_tokens(message.content) for message in prepared.request.messages
            ),
            "completion_tokens": estimate_tokens("".join(text)),
            "cost": None,
            # 與 `UsageDelta.estimated` 同義：查帳時分得出「量的」與「估的」。
            "estimated": True,
        }

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
        prepared: PreparedTurn | None,
        status: str = "completed",
    ) -> None:
        """正常收尾。`status` 只有中止時不是 `completed`——那時 `finish_reason` 是
        `stopped`，讓前端分得出「講完了」與「被停下來」（05 §3.4 的 `interrupted`）。"""
        answer = "".join(text)
        citations, rag_stats = self._citations(answer, prepared, message_id=turn.message_id)
        self._emit_trace(prepared, rag_stats)
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
        # **持久化之後是單向閥**：訊息已經是 completed 的事實，這之後的 Redis／DB
        # 抖動不得往上拋——拋出去會落進 `_generate` 的 except，`_fail` 把一則完整
        # 交付的回答改寫成 failed，且 `QuotaService.commit` 是非冪等的 incrby，第二次
        # settle 會把校正量套用兩遍。吞下的代價（done 事件可能沒送到、settle 失敗時
        # 預留量等期別翻頁才歸零）都遠輕於帳被改寫，且 client 靠重連／refetch 補得回。
        try:
            # usage_logs 落地（2A-1）。在 `done` **之前**：done 是前端停止讀取的訊
            # 號，也是測試查帳的時點，排在它後面的話「串流結束了、帳還沒到」是常態
            # 而不是異常。record 不往外拋（旁路原則，services/platform/usage.py），
            # 且自帶交易——不會污染 _persist 已經收尾的寫入。usage 為空（一個字都
            # 沒產生就停）時沒有數字可記，跳過而不是記一列 0。
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
            await self._budget.settle_tokens(turn.token_reservation, usage)
            await self._emit_citations(buffer, prepared, citations)
            await buffer.append(
                "done", {"message_id": str(turn.message_id), "finish_reason": finish_reason}
            )
            # 終局事件已送出——把緩衝區的壽命從 5 分鐘縮到 `stream_settled_ttl_seconds`
            # （二次架構審計 L1）。**必須在最後一個 append 之後**：append 每次都把 TTL
            # 重設回 5 分鐘。
            await buffer.settle()
        except Exception:
            logger.exception("chat_finalize_degraded", message_id=str(turn.message_id))
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
        prepared: PreparedTurn | None = None,
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
        # **失敗的回合一樣要開單據**：出事那天要查的正是這幾筆，而它們與成功的回合
        # 在檢索那一段沒有任何差別。
        self._emit_trace(prepared, rag_stats)
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
        # 同 `_complete` 的單向閥：狀態已持久化，之後的收尾失敗不得再走一次收尾
        # ——`_fail` 被 `_generate` 的 except 再包一層時，這裡拋出去就是第二次
        # settle（double-settle）加一次狀態改寫。
        try:
            await self._budget.settle_tokens(turn.token_reservation, usage)
            await self._emit_citations(buffer, prepared, citations)
            await buffer.append(
                "error", {"code": str(code), "title": message, "retryable": retryable}
            )
            # 同 `_complete`：終局事件之後縮短緩衝區壽命（L1）。失敗的回合同樣可能有
            # 一個正在重連的 client——它要看得到那則 error，而不是一個 409。
            await buffer.settle()
        except Exception:
            logger.exception("chat_finalize_degraded", message_id=str(turn.message_id))
        logger.warning(
            "chat_turn_failed",
            message_id=str(turn.message_id),
            code=str(code),
            status=status,
            produced_characters=sum(len(part) for part in text),
        )

    def _emit_trace(self, prepared: PreparedTurn | None, stats: dict[str, Any] | None) -> None:
        """一次問答**一筆** `rag_trace`（06 §7）。

        寫在收尾而不是檢索當下，因為 06 §7 明列的最後一項是「citation 驗證結果」——
        而那要等模型講完才知道。兩個地方各寫一筆的話，「這個月有多少 % 的查詢降級
        了」的分母會憑空變成兩倍，而兩筆都長得像真的。

        `prepared.trace` 是 `None` 代表這一輪沒有檢索（純閒聊路徑，06 §9）。**那時
        不寫**：記一筆全是 0 的單據會汙染所有比例型指標的分母（同 `usage.rag`
        的處置）。

        `stats` 是 `None` 而 trace 不是的情況也存在——模型一個字都沒產生就失敗了。
        那時檢索確實跑過，單據照開，只是沒有引用可驗。
        """
        if prepared is None or prepared.trace is None:
            return
        trace = prepared.trace
        if stats is not None:
            trace = trace.with_citations(
                citations=int(stats["citations"]), dropped=int(stats["dropped"])
            )
        emit_trace(trace)

    def _citations(
        self, answer: str, prepared: PreparedTurn | None, *, message_id: uuid.UUID
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
        prepared: PreparedTurn | None,
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

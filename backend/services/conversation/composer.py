"""TurnComposer —— 送進 LLM 之前把一次問答組起來（06 §3、1D-5；二次架構審計 F-07）。

**從 `ChatService` 切出來的第二塊。** 那個檔案在 2B 結束時是 814 行、建構子七個協作者，
而「組請求」這條線有自己完整的形狀：讀 system prompt、取近 N 輪歷史、跑檢索、把
context 拼進最後那則 user 訊息——四步結束就交出一個 `PreparedTurn`，之後再也不參與。

切開的第二個理由是 **3A（Tool 系統）**：工具的定義、可用性判斷與 schema 都要進到
送出去的請求裡，而那全部長在這一層。留在 `ChatService` 裡的話，那個檔案會在 3A 再
長幾百行，而它已經是全 repo 唯一同時 import `ai/`、`rag/`、`platform/` 的地方。

**組裝順序是 06 §3 的 system → memory → context → query**，而 context 與問題都在最後
那一則 `user` 訊息裡（10 §5 的指令／資料分域，見 `build_user_turn`）——這一點不能改：
把 context 放進 system 等於讓文件內容取得與規則同等的權威，那是 prompt injection
最直接的入口。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ai.gateway.chat import ChatMessage, ChatRequest
from ai.prompts import ContextChunk, build_context_block, build_user_turn
from core.db import run_orm
from core.tenant import tenant_context
from core.uow import unit_of_work
from rag.citation import marker_for
from rag.pipeline import build_search_query
from rag.retrievers.vector import RetrievedChunk
from rag.trace import RagTrace
from repositories.conversation import MessageRepository
from services.ai.prompts import SYSTEM_RAG_PROMPT_KEY, PromptService
from services.rag.retrieval import RetrievalOutcome, RetrievalService

__all__ = ["HISTORY_WINDOW_MESSAGES", "PreparedTurn", "TurnComposer"]

# 06 §5 的記憶視窗：近 10 輪。一問一答算兩則，所以取 20 則。
#
# 摘要壓縮（超出視窗的輪次併進 summary）屬 Phase 3C——在那之前，一場很長的對話會
# 直接失去更早的內容。這是**刻意的**：沒有摘要時的正確行為是「記得最近的」，而不是
# 「把全部塞進 context 直到爆掉」。
HISTORY_WINDOW_MESSAGES = 20


@dataclass(frozen=True, slots=True)
class PreparedTurn:
    """送出去之前組好的一切。`chunks` 留到收尾時比對引用（06 §3.3）。"""

    request: ChatRequest
    prompt_version: int
    chunks: tuple[RetrievedChunk, ...]
    retrieved: bool
    # 哪幾個增強步驟被跳過了（2B-3）——一路走到 `usage.rag.degraded`。
    degraded: tuple[str, ...] = ()
    # 這一趟檢索的單據（06 §7，2B-5）。**帶到收尾才寫出去**：引用的驗證結果是
    # 06 §7 明列的一項，而它要等模型講完才知道；檢索時先寫一筆、收尾再寫一筆的話，
    # 「這個月有多少 % 的查詢降級了」的分母會憑空變成兩倍。
    trace: RagTrace | None = None


class TurnComposer:
    def __init__(
        self,
        *,
        prompts: PromptService | None = None,
        retrieval: RetrievalService | None = None,
        messages: MessageRepository | None = None,
    ) -> None:
        self._prompts = prompts or PromptService()
        self._retrieval = retrieval or RetrievalService()
        self._messages = messages or MessageRepository()

    async def compose(
        self,
        tenant_id: uuid.UUID,
        *,
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        user_message_id: uuid.UUID,
        question: str,
        kb_ids: Sequence[uuid.UUID],
        model: str,
    ) -> PreparedTurn:
        """system prompt（1D-3b）+ 近 N 輪原文 + 檢索到的 context（1D-5）→ 請求。

        歷史從 DB 讀而不是由呼叫端傳：那是唯一一份真相，而「前端送什麼就用什麼」等於
        讓 client 決定 LLM 看得到哪些內容——它可以偽造一段從未發生過的對話。

        **收欄位而不是收 `TurnStarted`**：那個型別是 `chat.py` 兩段之間的契約，帶著
        額度預留與 user_id，而這一層一個都用不到。反過來 import 也會讓兩個模組互相
        依賴——組請求這件事不該認識「回合是怎麼建立的」。
        """
        rendered = await run_orm(self._prompts.render, tenant_id, key=SYSTEM_RAG_PROMPT_KEY)
        history, previous_questions = await run_orm(
            self._history,
            tenant_id,
            conversation_id=conversation_id,
            skip={message_id, user_message_id},
        )
        outcome = await self._retrieve(
            tenant_id, question=question, kb_ids=kb_ids, previous_questions=previous_questions
        )
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
            ChatMessage(role="user", content=build_user_turn(question, context)),
        ]
        return PreparedTurn(
            request=ChatRequest(messages=messages, model=model),
            prompt_version=rendered.version,
            chunks=tuple(chunks),
            retrieved=bool(kb_ids),
            degraded=outcome.degraded,
            trace=outcome.trace,
        )

    async def _retrieve(
        self,
        tenant_id: uuid.UUID,
        *,
        question: str,
        kb_ids: Sequence[uuid.UUID],
        previous_questions: Sequence[str],
    ) -> RetrievalOutcome:
        """這一輪的 context（06 §3）。沒掛 KB 就整段跳過——06 §9 的純閒聊路徑不付
        RAG 成本，而沒有 KB 時檢索一定查不到任何東西。

        回的是 `RetrievalOutcome` 而不是清單（2B-3）：降級標記要跟著走到
        `usage.rag.degraded`，而把它掛在 instance 上會在併發請求之間外洩——這個
        物件是跨請求共用的。
        """
        if not kb_ids:
            return RetrievalOutcome(chunks=[])

        kb_list = list(kb_ids)
        params = await run_orm(self._retrieval.params_for, tenant_id, kb_list)
        query = build_search_query(
            question,
            previous_questions=previous_questions,
            history_turns=params.query_history_turns,
        )
        return await run_orm(
            self._retrieval.retrieve_for_chat, tenant_id, kb_ids=kb_list, query=query
        )

    def _history(
        self,
        tenant_id: uuid.UUID,
        *,
        conversation_id: uuid.UUID,
        skip: set[uuid.UUID],
    ) -> tuple[list[ChatMessage], list[str]]:
        """近 N 輪 → (要送給模型的歷史, 先前的問題)。

        **這一輪的問題不在歷史裡**：它與 context 一起組成最後那則 user 訊息
        （`compose`），留在歷史裡會讓同一個問題出現兩次。還在生成的那一則也跳過
        ——它的內容是空的。

        先前的問題另外回傳，給檢索用（`build_search_query`）：「那病假呢？」單獨拿去
        搜命中的是一組無關內容，而模型那邊本來就看得到歷史，不需要改寫過的問句。
        """
        with tenant_context(tenant_id), unit_of_work():
            rows = self._messages.for_conversation(conversation_id, limit=HISTORY_WINDOW_MESSAGES)

        kept = [row for row in rows if row.content and uuid.UUID(str(row.id)) not in skip]
        return (
            [ChatMessage(role=_role_of(row.role), content=row.content) for row in kept],
            [str(row.content) for row in kept if row.role == "user"],
        )


def _role_of(role: str) -> Any:
    """DB 的 role 字串 → `ChatMessage` 的 role。

    未知的值退成 `user`：那比讓整次生成因為一個沒見過的字串而失敗好，而 05 §3.4 的
    四個值（user/assistant/tool/system）本來就涵蓋得了現在的寫入路徑。
    """
    return role if role in {"system", "user", "assistant", "tool"} else "user"

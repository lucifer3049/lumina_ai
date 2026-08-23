"""驗收：Chat 的 RAG 編排與引用（06 §3、09 §3.2，13 §3 工作包 1D-5）。

**這一包把 Phase 1 的價值迴路最後一段接上。** 1B 上傳、1C 算向量、1D-4 讓模型開口，
而在這之前它開口講的是自己的知識——使用者的文件從頭到尾沒有進過那次呼叫。這裡驗的
是「問題 → 檢索 → context → 回答 → 引用」整條路，以及它在幾種不理想的情況下不會
安靜地退化成一個看起來正常的系統。

分工：純函式的裁切與驗證在 `tests/unit/test_rag_pipeline.py`、
`tests/unit/test_citation.py`；本檔驗**接起來之後**的行為，因此走真的端點、真的
背景生成、真的 Redis 緩衝區。

六件事錯了都不會有例外：

1. **context 沒進到那次呼叫**。回答照樣通順（模型用自己的知識答），只是與使用者的
   文件無關——而那正是這整個產品要賣的東西。
2. **context 進了 system 而不是 user**。10 §5 的指令／資料分域消失，一份被污染的
   文件就能改寫我們的規則，而回答看起來完全正常。
3. **引用沒驗證**。模型憑空生的 id 一路到前端，長得與真來源一模一樣。
4. **引用沒存進 `messages.citations`**。重整頁面之後引用面板空了，而回答還在——
   使用者會以為那個答案本來就沒有依據。
5. **KB 不見了就整輪失敗**。對話是長命的，而 KB 可以在對話中途被刪掉：每一次發言
   都失敗，直到使用者放棄那場對話。
6. **別的租戶的 KB 被檢索到**。`kb_ids` 是對話建立時存下來的一串 id，而 RLS 只在
   repository 那一層擋得住——擋掉的東西必須是「什麼都查不到」，不是一個錯誤。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
import pytest

from ai.gateway import AIGateway
from ai.gateway.chat import (
    ChatRequest,
    ChatTimeouts,
    DoneDelta,
    ProviderDelta,
    TextDelta,
    UsageDelta,
)
from ai.gateway.providers import ProviderEmbedding
from ai.gateway.providers.mock import MockChatProvider, MockEmbeddingProvider
from ai.prompts import CONTEXT_END, CONTEXT_START
from api.main import create_app
from common.passwords import hash_password
from core.db import run_orm
from core.redis import get_redis, tenant_key
from services.knowledge.embedding import EmbeddingService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.conversation import make_conversation
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.factories.knowledge import make_chunk, make_document, make_knowledge_base
from tests.seed import ensure_identity_seed, ensure_prompt_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG_A = "tenant-a"
SLUG_B = "tenant-b"
OWNER_EMAIL = "owner@example.com"

_CHUNK_TEXT = "員工每年特別休假 14 天，應於三日前提出申請。"
_DOC_NAME = "人事規章.pdf"
_PAGE = 7


@pytest.fixture(autouse=True)
async def _drain_background_generation() -> AsyncIterator[None]:
    """等背景生成收工再讓測試結束（理由見 test_chat_stream.py 的同名 fixture）。"""
    yield
    from api.background import drain

    await drain(timeout_seconds=10)


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    for tenant_id in (TENANT_A, TENANT_B):
        keys = list(client.scan_iter(match=tenant_key(tenant_id, "*")))
        if keys:
            client.delete(*keys)


# ── 假 provider：把送給模型的東西錄下來 ───────────────────────────


class _Script:
    """錄下每一次 `stream_chat` 的請求，並決定模型要回什麼。

    **不是為了避開真 API**（MockProvider 已經在做那件事），是為了驗「送出去的內容
    長什麼樣」。`MockChatProvider` 會把 context 裡的第一個標記照抄回來，那足以驗出
    「有沒有引用」，但驗不出 **context 放在哪一個 role**——而 10 §5 的分域正是放錯
    位置就失效、且完全沒有症狀的那一類。
    """

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.reply: Callable[[ChatRequest], str] | None = None

    @property
    def last(self) -> ChatRequest:
        assert self.requests, "模型沒有被呼叫"
        return self.requests[-1]

    def message_of(self, role: str) -> str:
        return "\n".join(m.content for m in self.last.messages if m.role == role)

    def answer_for(self, request: ChatRequest) -> str:
        if self.reply is not None:
            return self.reply(request)
        return "（測試）依據提供的內容回答。"


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> _Script:
    scripted = _Script()

    async def stream_chat(
        self: Any, request: ChatRequest, *, timeouts: ChatTimeouts
    ) -> AsyncIterator[ProviderDelta]:
        scripted.requests.append(request)
        yield TextDelta(text=scripted.answer_for(request))
        yield UsageDelta(prompt_tokens=10, completion_tokens=5, model=request.model)
        yield DoneDelta(finish_reason="stop")

    monkeypatch.setattr(MockChatProvider, "stream_chat", stream_chat)
    return scripted


@pytest.fixture
def embedded_queries(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """錄下這次請求真正拿去檢索的字串。

    **這是黑箱的**：檢索的查詢一定會經過 embedding provider，所以攔在那裡就不必去
    偷看 service 的內部。文件的向量在 fixture 階段就算完了，因此請求期間唯一的
    embedding 呼叫就是查詢本身。
    """
    seen: list[str] = []
    original = MockEmbeddingProvider.embed

    def embed(
        self: Any, texts: list[str], *, model: str, timeout_seconds: float
    ) -> ProviderEmbedding:
        seen.extend(texts)
        return original(self, texts, model=model, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(MockEmbeddingProvider, "embed", embed)
    return seen


# ── 資料 ─────────────────────────────────────────────────────


def _seed_kb(tenant_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """一個有向量的 KB，回傳 (kb_id, document_id, chunk_id)。

    走真的 `EmbeddingService`（理由見 tests/integration/test_vector_retrieval.py）：
    自己塞一列 embedding 的話，驗不到「寫入與查詢用的是同一個模型與版本」——而那
    對不上時檢索永遠回空，回答會變成「知識庫中找不到相關內容」。
    """
    with tenant_scope(tenant_id):
        kb = make_knowledge_base(tenant_id=tenant_id)
        document = make_document(kb=kb, filename=_DOC_NAME, status="chunked")
        chunk = make_chunk(
            document=document,
            seq=0,
            content=_CHUNK_TEXT,
            meta={"page": _PAGE, "heading_path": ["人事規章", "請假"]},
        )
    EmbeddingService(
        gateway=AIGateway(embedding_provider=MockEmbeddingProvider(), retry_backoff_seconds=())
    ).embed_document(tenant_id, document.id)
    return uuid.UUID(str(kb.id)), uuid.UUID(str(document.id)), uuid.UUID(str(chunk.id))


def _owner(tenant_id: uuid.UUID, slug: str) -> uuid.UUID:
    from apps.identity.models import Role

    ensure_identity_seed()
    ensure_prompt_seed()
    with tenant_scope(tenant_id):
        make_tenant(id=tenant_id, slug=slug)
        user = make_user(
            tenant_id=tenant_id, email=OWNER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=user, role=Role.objects.get(tenant__isnull=True, name="owner"))
    return uuid.UUID(str(user.id))


@pytest.fixture
def owner() -> uuid.UUID:
    return _owner(TENANT_A, SLUG_A)


@pytest.fixture
def knowledge() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    return _seed_kb(TENANT_A)


def _conversation(owner_id: uuid.UUID, kb_ids: list[uuid.UUID]) -> uuid.UUID:
    with tenant_scope(TENANT_A):
        conversation = make_conversation(tenant_id=TENANT_A, user_id=owner_id, kb_ids=kb_ids)
    return uuid.UUID(str(conversation.id))


@pytest.fixture
def grounded_chat(owner: uuid.UUID, knowledge: tuple[uuid.UUID, uuid.UUID, uuid.UUID]) -> uuid.UUID:
    """掛著知識庫的一場對話——RAG 路徑。"""
    return _conversation(owner, [knowledge[0]])


@pytest.fixture
def plain_chat(owner: uuid.UUID) -> uuid.UUID:
    """沒掛任何知識庫的一場對話——06 §9 的純閒聊路徑。"""
    return _conversation(owner, [])


@pytest.fixture
def chat_with_a_deleted_kb(owner: uuid.UUID) -> uuid.UUID:
    """掛著一個不存在的 KB——它在對話中途被刪掉了。"""
    return _conversation(owner, [uuid.uuid4()])


@pytest.fixture
def chat_with_another_tenants_kb(owner: uuid.UUID) -> uuid.UUID:
    """掛著**租戶 B** 的 KB。`kb_ids` 是一串沒有人驗證過歸屬的 id。

    **建資料一律在同步 fixture 裡**：Django ORM 是同步的，在 async 測試函式裡直接
    建會被 `SynchronousOnlyOperation` 擋下（同其他 api 測試把資料 fixture 寫成同步的
    理由）。
    """
    _owner(TENANT_B, SLUG_B)
    other_kb, _, _ = _seed_kb(TENANT_B)
    return _conversation(owner, [other_kb])


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


# ── 操作 ─────────────────────────────────────────────────────


async def _token(client: httpx.AsyncClient, slug: str = SLUG_A) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": slug, "email": OWNER_EMAIL, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _ask(
    client: httpx.AsyncClient,
    conversation_id: uuid.UUID,
    token: str,
    *,
    content: str = "年假幾天？",
) -> list[tuple[str, dict[str, Any]]]:
    created = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=_auth(token),
        json={"content": content},
    )
    assert created.status_code == 201, created.text
    message_id = created.json()["message_id"]

    events: list[tuple[str, dict[str, Any]]] = []
    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/messages/{message_id}/stream",
        headers={**_auth(token), "Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, await response.aread()
        name = ""
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                events.append((name, json.loads(line.removeprefix("data: "))))
    return events


def _names(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _ in events]


def _payload(events: list[tuple[str, dict[str, Any]]], name: str) -> dict[str, Any]:
    for event_name, data in events:
        if event_name == name:
            return data
    raise AssertionError(f"沒有 {name} 事件：{_names(events)}")


def _text(events: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(str(data["text"]) for name, data in events if name == "delta")


async def _stored_answer(conversation_id: uuid.UUID) -> Any:
    return await run_orm(_load_answer, conversation_id)


def _load_answer(conversation_id: uuid.UUID) -> Any:
    from apps.conversation.models import Message

    with tenant_scope(TENANT_A):
        return (
            Message.objects.filter(conversation_id=conversation_id, role="assistant")
            .order_by("-created_at")
            .first()
        )


# ── 驗收 ─────────────────────────────────────────────────────


class TestContextReachesTheModel:
    async def test_the_retrieved_chunk_is_in_the_request(
        self, client: httpx.AsyncClient, script: _Script, grounded_chat: uuid.UUID
    ) -> None:
        """**這是整包的重點。** 沒有這一條，模型答得再流暢也與使用者的文件無關。"""
        await _ask(client, grounded_chat, await _token(client))

        assert _CHUNK_TEXT in script.message_of("user")

    async def test_the_context_goes_to_the_user_role_between_delimiters(
        self,
        client: httpx.AsyncClient,
        script: _Script,
        grounded_chat: uuid.UUID,
    ) -> None:
        """指令放 system、外部資料只進 user 的 context 區塊（10 §5）。

        混在同一個 role 裡的話，一份被污染的文件就能用「忽略以上指令」改寫我們的
        規則——而回答看起來完全正常。定界標記是這道防線的另一半。
        """
        await _ask(client, grounded_chat, await _token(client))

        user_message = script.message_of("user")
        assert CONTEXT_START in user_message
        assert CONTEXT_END in user_message
        # 每一段都帶著自己的引用標記，模型才有東西可以照抄。標記是**本輪的第幾段**
        # 而不是 chunk 的 UUID（2026-08-17 決定，理由見 tests/unit/test_citation.py）。
        assert "[c:1]" in user_message
        assert CONTEXT_START not in script.message_of("system")

    async def test_the_question_comes_after_the_context(
        self, client: httpx.AsyncClient, script: _Script, grounded_chat: uuid.UUID
    ) -> None:
        """06 §3 的組裝順序：system → memory → context → query。

        問題排在 context 之前的話，模型讀到問題時還不知道有哪些資料；而把問題埋在
        一大段 context 之前也讓 prompt cache 的穩定前綴（06 §4）從第一個 token 就
        失效——每一輪都是一次全新的 prompt。
        """
        await _ask(client, grounded_chat, await _token(client), content="年假幾天？")

        user_message = script.message_of("user")
        assert user_message.index(CONTEXT_END) < user_message.index("年假幾天？")

    async def test_a_conversation_without_a_knowledge_base_skips_retrieval(
        self, client: httpx.AsyncClient, script: _Script, plain_chat: uuid.UUID
    ) -> None:
        """純閒聊路徑不付 RAG 成本（06 §9）——一次 embedding 呼叫也是錢，而沒有
        KB 時它一定查不到任何東西。"""
        events = await _ask(client, plain_chat, await _token(client))

        assert CONTEXT_START not in script.message_of("user")
        assert "citations" not in _names(events)

    async def test_a_follow_up_question_searches_with_the_previous_one(
        self,
        client: httpx.AsyncClient,
        script: _Script,
        embedded_queries: list[str],
        grounded_chat: uuid.UUID,
    ) -> None:
        """追問要帶著上一個問題去檢索（06 §3.1 的 condense，**免錢版**）。

        「那病假呢？」單獨拿去搜，命中的是一組與請假無關的內容——而模型會很有禮貌
        地依據那些內容回答。文件的做法是用小模型改寫成獨立問句，那是每一輪多一次
        LLM 呼叫；1D-5 先做零成本的版本：把前一個問題接上去一起搜。真正的 condense
        排 Phase 2/3C，那時有 golden set 量得出它好多少。
        """
        token = await _token(client)
        await _ask(client, grounded_chat, token, content="年假幾天？")
        embedded_queries.clear()

        await _ask(client, grounded_chat, token, content="那病假呢？")

        assert embedded_queries, "第二輪沒有做檢索"
        assert "年假" in embedded_queries[0]
        assert "病假" in embedded_queries[0]


class TestCitationEvent:
    async def test_it_arrives_before_done(
        self,
        client: httpx.AsyncClient,
        script: _Script,
        grounded_chat: uuid.UUID,
        knowledge: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    ) -> None:
        """09 §3.2 的事件順序：`citations` 在 `done` 之前。

        `done` 是 client 停止讀取的訊號（`_TERMINAL_EVENTS`）——排在它後面的事件
        永遠不會被收到，而串流看起來完全正常。
        """
        script.reply = lambda _: "年假 14 天 [c:1]。"

        events = _names(await _ask(client, grounded_chat, await _token(client)))

        assert "citations" in events
        assert events.index("citations") < events.index("done")

    async def test_the_payload_points_back_to_the_source(
        self,
        client: httpx.AsyncClient,
        script: _Script,
        grounded_chat: uuid.UUID,
        knowledge: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    ) -> None:
        """引用面板要說得出「出自哪份文件、第幾頁、哪一章節」（06 §3.3、09 §3.2）。

        `marker` 是答案文字裡那個 `[c:1]`——前端靠它把標記換成可以點的上標。
        `heading_path` 與 `doc_version` 是 2026-08-17 加的：前者是 Markdown／xlsx
        唯一說得出位置的東西（那兩種沒有頁碼），後者讓文件重新上傳之後，這則舊回答
        仍說得出當時引用的是第幾版。兩筆資料本來就在手上，先前只是沒送出去。
        """
        _, document_id, chunk_id = knowledge
        script.reply = lambda _: "年假 14 天 [c:1]。"

        events = await _ask(client, grounded_chat, await _token(client))

        items = _payload(events, "citations")["items"]
        assert items == [
            {
                "marker": "1",
                "chunk_id": str(chunk_id),
                "doc_id": str(document_id),
                "doc_name": _DOC_NAME,
                "doc_version": 1,
                "page": _PAGE,
                "heading_path": ["人事規章", "請假"],
                "score": pytest.approx(items[0]["score"]),
                "snippet": _CHUNK_TEXT,
            }
        ]

    async def test_a_hallucinated_marker_is_dropped(
        self,
        client: httpx.AsyncClient,
        script: _Script,
        grounded_chat: uuid.UUID,
        knowledge: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    ) -> None:
        """憑空的 id 不進引用清單（06 §3.3）。

        **但答案的文字一個字都不改。** 已經逐字送出去的東西收不回來，而重寫持久化的
        內容會讓「使用者看到的」與「資料庫存的」不一致——那是 1D-4a 特地釘住的一條
        不變式（重整之後回答不該變）。標記留在文字裡而清單裡沒有它，前端因此渲染成
        普通文字，那是這裡能給的最誠實的結果。
        """
        _, _, chunk_id = knowledge
        script.reply = lambda _: "甲 [c:1]。乙 [c:9]。"

        events = await _ask(client, grounded_chat, await _token(client))

        items = _payload(events, "citations")["items"]
        assert [item["chunk_id"] for item in items] == [str(chunk_id)]
        assert "[c:9]" in _text(events)

    async def test_citations_are_persisted_on_the_message(
        self,
        client: httpx.AsyncClient,
        script: _Script,
        grounded_chat: uuid.UUID,
    ) -> None:
        """重整頁面之後引用面板還在（05 §3.4 的 `citations jb`）。

        只送事件不落地的話，引用會在關掉分頁的那一刻消失——而回答還在，於是那個
        答案看起來從一開始就沒有依據。
        """
        script.reply = lambda _: "年假 14 天 [c:1]。"

        events = await _ask(client, grounded_chat, await _token(client))

        stored = await _stored_answer(grounded_chat)
        assert stored.citations == _payload(events, "citations")["items"]

    async def test_the_api_returns_them(
        self,
        client: httpx.AsyncClient,
        script: _Script,
        grounded_chat: uuid.UUID,
        knowledge: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    ) -> None:
        """`GET /messages` 帶得回引用——1E 的引用面板在重新載入之後靠它。"""
        _, _, chunk_id = knowledge
        script.reply = lambda _: "年假 14 天 [c:1]。"
        token = await _token(client)

        await _ask(client, grounded_chat, token)
        listed = await client.get(
            f"/api/v1/conversations/{grounded_chat}/messages", headers=_auth(token)
        )

        answer = listed.json()["items"][-1]
        assert [item["chunk_id"] for item in answer["citations"]] == [str(chunk_id)]

    async def test_an_answer_with_no_marker_still_reports_the_absence(
        self, client: httpx.AsyncClient, script: _Script, grounded_chat: uuid.UUID
    ) -> None:
        """檢索跑過但模型沒引用任何東西時，仍然送一個**空的** citations 事件。

        不送的話，前端分不出「這是純閒聊」與「查了但沒有依據」——後者要顯示的是
        「本回答未引用知識庫內容」，那是一個提醒，不是一個空白。
        """
        script.reply = lambda _: "知識庫中找不到相關內容。"

        events = await _ask(client, grounded_chat, await _token(client))

        assert _payload(events, "citations")["items"] == []


class TestCitationStats:
    """這一輪的三個數字要落地（2026-08-17 決定）。

    **只進 log 等於沒人會看**：要回答「這個月有多少 % 的回答出現假引用」得去翻幾百萬
    行日誌，而那件事沒有人會做第二次。落在 `messages.usage` 裡則是一句 SQL。

    它同時是 13 §3.5 第 1 項（引用標記改用短編號）唯一的量測——那個決定的前提是
    「模型抄一位數不會錯」，而現在用的是假模型，抄得對是理所當然的。真 provider 接上
    （1C-5）之後，`dropped` 的趨勢就是那個前提成不成立的答案；**現在不記，那時就沒有
    歷史可比**——歷史補不回來。
    """

    async def test_they_land_on_the_message(
        self, client: httpx.AsyncClient, script: _Script, grounded_chat: uuid.UUID
    ) -> None:
        script.reply = lambda _: "甲 [c:1]。乙 [c:9]。"

        await _ask(client, grounded_chat, await _token(client))

        stored = await _stored_answer(grounded_chat)
        # `degraded` 是 2B-3 加的：rerank／FTS 被跳過時要說得出來，否則「檢索品質變差」
        # 在任何地方都查不到，而看得到的只有評測分數掉了一截。正常路徑是空清單。
        assert stored.usage["rag"] == {
            "context_chunks": 1,
            "citations": 1,
            "dropped": 1,
            "degraded": [],
        }

    async def test_they_do_not_disturb_the_token_counts(
        self, client: httpx.AsyncClient, script: _Script, grounded_chat: uuid.UUID
    ) -> None:
        """**放在 `usage["rag"]` 子物件裡，不與 token 平放。** `prompt_tokens` 那幾個
        鍵是 2A 計費的原料，混在一起遲早會有人把 `dropped` 當成一種 token。"""
        await _ask(client, grounded_chat, await _token(client))

        usage = (await _stored_answer(grounded_chat)).usage
        assert usage["prompt_tokens"] == 10
        assert usage["completion_tokens"] == 5

    async def test_a_conversation_without_retrieval_has_no_stats(
        self, client: httpx.AsyncClient, script: _Script, plain_chat: uuid.UUID
    ) -> None:
        """純閒聊路徑沒有檢索，也就沒有東西可統計——**記一組全是 0 的數字會汙染
        分母**，讓「有多少 % 的回答出現假引用」把沒查過知識庫的那些也算進去。"""
        await _ask(client, plain_chat, await _token(client))

        assert "rag" not in (await _stored_answer(plain_chat)).usage


class TestDegradation:
    async def test_a_knowledge_base_that_is_gone_does_not_break_the_turn(
        self, client: httpx.AsyncClient, script: _Script, chat_with_a_deleted_kb: uuid.UUID
    ) -> None:
        """KB 可以在對話中途被刪掉，而對話是長命的。

        整輪失敗的話，那場對話從此每一次發言都失敗，直到使用者放棄它——而使用者
        看到的只是「一直出錯」。正確的行為是照樣回答（沒有 context，模型會依模板
        規則 3 說自己不知道），並在 log 留下線索。
        """
        events = await _ask(client, chat_with_a_deleted_kb, await _token(client))

        assert _names(events)[-1] == "done"
        assert _payload(events, "citations")["items"] == []

    async def test_another_tenants_knowledge_base_contributes_nothing(
        self,
        client: httpx.AsyncClient,
        script: _Script,
        chat_with_another_tenants_kb: uuid.UUID,
    ) -> None:
        """**跨租戶的 kb_id 必須什麼都查不到。**

        `kb_ids` 是對話建立時存下來的一串 id，沒有任何東西保證它們屬於這個租戶。
        擋下來的位置是 repository 的租戶 filter 與 RLS，而這條測試驗的是「擋掉之後
        的行為是空的 context」——租戶 B 的文件內容一個字都不能出現在送給模型的請求裡。
        """
        events = await _ask(client, chat_with_another_tenants_kb, await _token(client))

        assert _CHUNK_TEXT not in script.message_of("user")
        assert _payload(events, "citations")["items"] == []
        assert _names(events)[-1] == "done"

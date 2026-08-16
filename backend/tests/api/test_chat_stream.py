"""驗收：發送訊息 → SSE 串流回應（09 §2.4／§3.2、13 §3 工作包 1D-4a）。

**這是整個 Phase 1 的價值迴路第一次接通**：1B 上傳、1C 檢索、1D-3a 說話、1D-3b 知道
要說什麼，而在這一包之前，使用者按下送出仍然沒有任何反應。

**端點拆成兩步（2026-08-16 決策，偏離 09 §2.4 的單一 POST）**：

    POST /conversations/{id}/messages              → 201 {message_id}（建立回合、開始生成）
    GET  /conversations/{id}/messages/{mid}/stream  → SSE

原設計是「POST 直接回串流」。改成兩步的理由不是形式，是**一個正確性問題**：那個 POST
同時做了建立與串流兩件事，而網路閃斷時 client 分不出「單子送出去了沒」——重送一次就
是兩則訊息、兩次生成、兩次帳單，而 09 §2.4 的冪等鍵（★）並沒有標在這個端點上。拆開
之後，建立是一個普通的 JSON 請求（冪等鍵掛得上去），串流是一個可以重複讀的資源。

連帶三件事變自然：生成前的失敗就是 POST 的 HTTP 錯誤碼；resume 就是**再 GET 一次同一
個網址**（1D-4b 不必為它開第二條路徑）；client 斷線後生成繼續（G-06）本來就成立，
因為生成不掛在那條連線上。

（原設計選 POST 是為了配合前端的 `EventSource`。實際上 `EventSource` 不能帶
`Authorization` header，而我們的憑證正是 Bearer——那條路無論 GET 或 POST 都走不通，
前端一律走 fetch + ReadableStream。03 §2 與 09 §2.4 需同步修訂。）

本包不含檢索（1D-5）、不含 resume 與 stop（1D-4b）。四件事錯了都不會有例外：

1. **持久化與串流不一致**。使用者看到的字與資料庫存的字不同——重新整理之後回答變了，
   而兩邊的程式碼各自看起來都對。
2. **失敗時使用者的問題也不見了**。user 訊息要在生成**之前**就落地：生成失敗是常態
   （provider 掛掉、配額用盡），而問題消失會讓使用者以為自己沒送出。
3. **快照沒留**。`model` 與 `prompt_version`（05 §3.4）是「這個回答當時用了什麼」的
   唯一紀錄，漏記的話 3B 的評測與事故回溯都失去依據。
4. **串流端點漏掉擁有者判定**。拆成兩步之後多了一個入口，而 RLS 只擋租戶、擋不了
   同租戶的另一個使用者（1D-2 已經踩過這條）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from api.main import create_app
from common.passwords import hash_password
from core.db import run_orm
from core.redis import get_redis, tenant_key
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.conversation import make_conversation
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.seed import ensure_identity_seed, ensure_prompt_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG_A = "tenant-a"
OWNER_EMAIL = "owner@example.com"
OTHER_EMAIL = "someone-else@example.com"


@pytest.fixture(autouse=True)
async def _drain_background_generation() -> AsyncIterator[None]:
    """等背景的生成 task 收工再讓測試結束。

    **不等的話，那個 task 會活過測試本體**，而 `transaction=True` 的 teardown 正在
    TRUNCATE 全部的表——兩邊撞在一起的症狀是「下一條測試的 fixture 撞唯一鍵」，受害者
    完全不指向真因（1D-2 的結案紀錄描述過同一種形狀）。

    正式環境的對應物是 graceful shutdown（11 §196：SIGTERM → 送 error 事件 → 等 ≤30s），
    屬 1D-4b。這裡先讓測試本身是確定性的。
    """
    yield
    from api.v1 import conversations as endpoint

    pending = set(endpoint._running)
    if pending:
        await asyncio.wait(pending, timeout=10)


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    for tenant_id in (TENANT_A, TENANT_B):
        keys = list(client.scan_iter(match=tenant_key(tenant_id, "*")))
        if keys:
            client.delete(*keys)


@pytest.fixture
def conversation() -> uuid.UUID:
    """租戶 A 的 owner 與他的一場對話（外加一個同租戶的其他使用者）。"""
    ensure_identity_seed()
    ensure_prompt_seed()
    from apps.identity.models import Role

    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG_A)
        owner = make_user(
            tenant_id=TENANT_A, email=OWNER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        other = make_user(
            tenant_id=TENANT_A, email=OTHER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        role = Role.objects.get(tenant__isnull=True, name="owner")
        for user in (owner, other):
            make_user_role(user=user, role=role)
        chat = make_conversation(tenant_id=TENANT_A, user_id=owner.id)
    return uuid.UUID(str(chat.id))


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


async def _token(client: httpx.AsyncClient, email: str = OWNER_EMAIL, slug: str = SLUG_A) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": slug, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, f"{email} 登入失敗：{response.text}"
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _send(
    client: httpx.AsyncClient,
    conversation_id: uuid.UUID,
    token: str,
    *,
    content: str = "年假幾天？",
    stream: bool = True,
) -> httpx.Response:
    """第一步：建立回合。回應是普通的 JSON，不是串流。"""
    suffix = "" if stream else "?stream=false"
    return await client.post(
        f"/api/v1/conversations/{conversation_id}/messages{suffix}",
        headers=_auth(token),
        json={"content": content},
    )


async def _read_stream(
    client: httpx.AsyncClient, conversation_id: uuid.UUID, message_id: str, token: str
) -> list[tuple[str, dict[str, Any]]]:
    """第二步：讀串流。回傳 [(事件名, payload)]。"""
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


async def _ask(
    client: httpx.AsyncClient,
    conversation_id: uuid.UUID,
    token: str,
    *,
    content: str = "年假幾天？",
) -> list[tuple[str, dict[str, Any]]]:
    created = await _send(client, conversation_id, token, content=content)
    assert created.status_code == 201, created.text
    return await _read_stream(client, conversation_id, created.json()["message_id"], token)


def _text_of(events: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(payload["text"] for name, payload in events if name == "delta")


async def _messages(conversation_id: uuid.UUID) -> list[Any]:
    """讀回這場對話的訊息。**經 run_orm**：ORM 是同步的，在 async 測試裡直接呼叫會被
    Django 以 `SynchronousOnlyOperation` 擋下（同其他 api 測試把資料 fixture 寫成同步的
    理由）。"""
    return await run_orm(_load_messages, conversation_id)


def _load_conversation(conversation_id: uuid.UUID) -> Any:
    from apps.conversation.models import Conversation

    with tenant_scope(TENANT_A):
        return Conversation.objects.get(id=conversation_id)


def _load_messages(conversation_id: uuid.UUID) -> list[Any]:
    from apps.conversation.models import Message

    with tenant_scope(TENANT_A):
        return list(Message.objects.filter(conversation_id=conversation_id).order_by("created_at"))


class TestCreateTurn:
    async def test_it_returns_the_message_id(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """**client 在收到任何一個位元組之前就知道 message_id。**

        它是後續三件事的定位鍵：讀串流、按停止（1D-4b）、以及斷線後直接抓最終訊息。
        沒有它的話，client 只能等 `meta` 事件——而生成失敗時那個事件永遠不會來。
        """
        response = await _send(client, conversation, await _token(client))

        assert response.status_code == 201
        assert uuid.UUID(response.json()["message_id"])

    async def test_it_does_not_block_on_generation(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """建立回合就回，不等 LLM。等的話這一步會吃掉整個 TTFT 預算，而拆成兩步的
        意義就沒了。"""
        response = await _send(client, conversation, await _token(client))

        assert response.headers["content-type"].startswith("application/json")

    async def test_the_question_is_stored_immediately(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """user 訊息要在生成之前就落地：生成失敗是常態，而問題消失會讓使用者以為
        自己沒送出。"""
        await _send(client, conversation, await _token(client), content="年假幾天？")

        stored = await _messages(conversation)
        assert stored[0].role == "user"
        assert stored[0].content == "年假幾天？"

    async def test_an_empty_question_is_rejected(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """空問題不該花一次 LLM 呼叫的錢。**這類驗證留在第一步是拆開的好處之一**：
        它是普通的 422，不必包成串流事件。"""
        response = await _send(client, conversation, await _token(client), content="   ")

        assert response.status_code == 422
        assert (await _messages(conversation)) == []


class TestStreamProtocol:
    async def test_the_response_is_an_event_stream(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        token = await _token(client)
        created = await _send(client, conversation, token)

        async with client.stream(
            "GET",
            f"/api/v1/conversations/{conversation}/messages/{created.json()['message_id']}/stream",
            headers={**_auth(token), "Accept": "text/event-stream"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            await response.aread()

    async def test_meta_comes_first(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """`meta` 帶的是這次生成**實際**用的模型（fallback 鏈可能換過，1D-3a）——
        那與 client 送出時以為的可能不同。"""
        events = await _ask(client, conversation, await _token(client))

        assert events[0][0] == "meta"
        assert events[0][1]["model"]

    async def test_done_comes_last_and_once(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        events = await _ask(client, conversation, await _token(client))

        assert events[-1][0] == "done"
        assert sum(1 for name, _ in events if name == "done") == 1

    async def test_usage_precedes_done(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """順序沿用 1D-3a 對 Gateway 的保證，端點不得把它重排（2A 的計費靠它）。"""
        names = [name for name, _ in await _ask(client, conversation, await _token(client))]

        assert names.index("usage") < names.index("done")

    async def test_the_answer_arrives_in_pieces(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """一次吐完的話，逐字顯示與 TTFT（11 §1 的 p95 < 3.5s）都失去意義。"""
        events = await _ask(client, conversation, await _token(client))

        assert sum(1 for name, _ in events if name == "delta") > 1
        assert _text_of(events)

    async def test_a_late_reader_still_gets_everything_from_the_start(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """**晚一點才來讀，不會少拿。** 生成在 POST 當下就開始了，而 client 可能過
        一會兒才接上（慢網路、頁面還在載入）。少拿的話，回答會從中間開始。

        這條同時是 1D-4b 的 resume 能成立的前提：讀取端與產生端本來就是解耦的。
        """
        token = await _token(client)
        created = await _send(client, conversation, token)
        message_id = created.json()["message_id"]

        first = await _read_stream(client, conversation, message_id, token)
        again = await _read_stream(client, conversation, message_id, token)

        assert _text_of(first) == _text_of(again), "同一條串流讀兩次應該拿到同樣的內容"


class TestPersistence:
    async def test_what_the_user_saw_is_what_got_stored(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """**串流與持久化必須是同一份內容。** 不同的話，重新整理之後回答會變，
        而兩邊的程式碼各自看起來都對。"""
        events = await _ask(client, conversation, await _token(client))

        assert (await _messages(conversation))[-1].content == _text_of(events)

    async def test_the_assistant_message_completes(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """05 §3.4 的狀態機：streaming → completed。停在 streaming 的話，前端會把一則
        早就講完的訊息永遠顯示成「正在輸入」。"""
        await _ask(client, conversation, await _token(client))

        assert (await _messages(conversation))[-1].status == "completed"

    async def test_the_snapshot_records_model_and_prompt_version(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """05 §3.4 的生成快照。漏記的話「這個回答當時用了什麼」永遠答不出來——而那是
        06 §1 的版本化貫穿要保證的事（1D-3b 的整個不可變性也是為了它）。"""
        await _ask(client, conversation, await _token(client))

        assistant = (await _messages(conversation))[-1]
        assert assistant.model
        assert assistant.prompt_version is not None

    async def test_usage_is_persisted(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """2A 的計費原料。SSE 送出去而沒存進 DB 的話，事後對帳沒有任何依據。"""
        await _ask(client, conversation, await _token(client))

        usage = (await _messages(conversation))[-1].usage
        assert usage.get("prompt_tokens", 0) > 0
        assert usage.get("completion_tokens", 0) > 0

    async def test_the_conversation_counters_move(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """05 §6 的反正規化：一問一答 = 兩則。不更新的話，剛講完話的對話不會浮到
        列表最上面，而使用者會以為自己的訊息沒送出。"""
        await _ask(client, conversation, await _token(client))

        row = await run_orm(_load_conversation, conversation)
        assert row.message_count == 2
        assert row.last_message_at is not None


class TestHistoryWindow:
    async def test_previous_turns_reach_the_model(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """近 N 輪原文要進 prompt（06 §5 的視窗版；摘要壓縮屬 Phase 3）。

        不帶的話，每一輪都是全新的對話——使用者問「那它呢？」時模型完全接不上，
        而那看起來像模型很笨，不像我們沒把上下文送出去。
        """
        token = await _token(client)
        await _ask(client, conversation, token, content="第一個問題")

        events = await _ask(client, conversation, token, content="第二個問題")

        # MockChatProvider 會把收到的問句回抄（決定性），因此看得出送進去的是哪一輪。
        assert "第二個問題" in _text_of(events)
        assert len(await _messages(conversation)) == 4


class TestNonStreamingMode:
    async def test_it_returns_the_finished_message(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """09 §2.4 的 `?stream=false`：整合方（機器）不需要逐字，只要答案。
        這一步會等生成完成再回，因此回的是**完整訊息**而不是 message_id。"""
        response = await _send(client, conversation, await _token(client), stream=False)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["content"]

    async def test_it_persists_the_same_way(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """**兩種模式共用同一條生成路徑**（09 §5 已把它標為技術債）。

        分成兩條的話，其中一條的持久化、計費或快照遲早會漏一項，而那條路徑的使用者
        是整合方——最不會回報問題的那一群。
        """
        await _send(client, conversation, await _token(client), stream=False)

        assistant = (await _messages(conversation))[-1]
        assert assistant.status == "completed"
        assert assistant.model and assistant.prompt_version is not None
        assert assistant.usage.get("completion_tokens", 0) > 0


class TestFailures:
    async def test_a_failure_before_any_token_is_reported_on_the_stream(
        self, client: httpx.AsyncClient, conversation: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """生成是在 POST 之後才跑的，所以 provider 掛掉一律在串流上呈現——09 附錄 A
        對 PROVIDER_UNAVAILABLE 寫的正是「SSE 中以 error event 呈現」。

        code 與中途斷掉的那一種**必須分得開**：這一種一個字都沒產生（重試是乾淨的），
        另一種已經交付了半段（重試會看到兩個開頭）。
        """
        _break_provider(monkeypatch, before_first_token=True)

        events = await _ask(client, conversation, await _token(client))

        assert events[-1][0] == "error"
        assert events[-1][1]["code"] == "PROVIDER_UNAVAILABLE"

    async def test_a_failure_before_any_token_marks_the_message_failed(
        self, client: httpx.AsyncClient, conversation: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """05 §3.4 的 `failed`：一個字都沒產生。停在 streaming 的話，那則訊息會在
        使用者的歷史裡永遠「正在輸入」。"""
        _break_provider(monkeypatch, before_first_token=True)

        await _ask(client, conversation, await _token(client))

        assistant = (await _messages(conversation))[-1]
        assert assistant.status == "failed"
        assert assistant.content == ""

    async def test_a_failure_mid_stream_keeps_the_partial_answer(
        self, client: httpx.AsyncClient, conversation: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """09 附錄 A 對 STREAM_INTERRUPTED 寫的是「partial 已保存」——使用者按重新
        生成之前，那半句話要還在畫面上也還在歷史裡。"""
        _break_provider(monkeypatch, before_first_token=False)

        events = await _ask(client, conversation, await _token(client))

        assistant = (await _messages(conversation))[-1]
        assert events[-1][1]["code"] == "STREAM_INTERRUPTED"
        assert assistant.status == "interrupted"
        assert assistant.content == _text_of(events)
        assert assistant.content, "中斷前已經產生的內容不該被丟掉"


class TestAuthorization:
    async def test_another_user_cannot_send(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """擁有者制（1D-2）：同租戶的另一個使用者也不行，而且回 404 不是 403——
        403 等於承認那個 id 存在。"""
        token = await _token(client, email=OTHER_EMAIL)

        response = await _send(client, conversation, token)

        assert response.status_code == 404
        assert (await _messages(conversation)) == []

    async def test_another_user_cannot_read_the_stream(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """**拆成兩步多了一個入口，而這個入口最容易漏掉判定。**

        RLS 只擋租戶，擋不了同租戶的另一個使用者（1D-2 已經踩過這條）。漏掉的話，
        知道 message_id 的人就讀得到別人正在生成的回答——而 message_id 會出現在
        前端的網址與 log 裡。
        """
        owner_token = await _token(client)
        created = await _send(client, conversation, owner_token)
        intruder = await _token(client, email=OTHER_EMAIL)

        response = await client.get(
            f"/api/v1/conversations/{conversation}/messages/{created.json()['message_id']}/stream",
            headers={**_auth(intruder), "Accept": "text/event-stream"},
        )

        assert response.status_code == 404

    async def test_an_unknown_message_is_not_found(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        token = await _token(client)

        response = await client.get(
            f"/api/v1/conversations/{conversation}/messages/{uuid.uuid4()}/stream",
            headers={**_auth(token), "Accept": "text/event-stream"},
        )

        assert response.status_code == 404

    async def test_an_unknown_conversation_is_not_found(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        response = await _send(client, uuid.uuid4(), await _token(client))

        assert response.status_code == 404

    @pytest.mark.parametrize("method", ["send", "stream"])
    async def test_both_entrances_require_authentication(
        self, client: httpx.AsyncClient, conversation: uuid.UUID, method: str
    ) -> None:
        if method == "send":
            response = await client.post(
                f"/api/v1/conversations/{conversation}/messages", json={"content": "嗨"}
            )
        else:
            response = await client.get(
                f"/api/v1/conversations/{conversation}/messages/{uuid.uuid4()}/stream"
            )

        assert response.status_code == 401


def _break_provider(monkeypatch: pytest.MonkeyPatch, *, before_first_token: bool) -> None:
    """讓 chat provider 在指定的時機失敗。

    **不打真 API**（CLAUDE.md）：這裡換掉的是 `build_gateway` 解析出來的那個 provider，
    驗的是「端點怎麼處理失敗」，與 provider 是誰無關。
    """
    from collections.abc import AsyncGenerator

    from ai.gateway.chat import ChatRequest, ChatTimeouts, ProviderDelta, TextDelta
    from core.exceptions import ProviderUnavailableError

    class _BrokenProvider:
        name = "broken"

        async def stream_chat(
            self, request: ChatRequest, *, timeouts: ChatTimeouts
        ) -> AsyncGenerator[ProviderDelta, None]:
            if not before_first_token:
                yield TextDelta(text="講到一半")
            raise ProviderUnavailableError("provider 掛了")

    from api.v1 import conversations as endpoint

    monkeypatch.setattr("ai.gateway._chat_provider", lambda name: _BrokenProvider(), raising=True)
    # **`ChatService` 會把 Gateway 快取起來**（惰性建立，見該檔）。而端點模組的
    # `_chat` 是 module 層單例，前一條測試早就把它建好了——只 patch provider 解析
    # 函式的話，這裡換的東西根本沒有人會再讀。清掉快取讓它重建一次。
    monkeypatch.setattr(endpoint._chat, "_gateway", None, raising=False)

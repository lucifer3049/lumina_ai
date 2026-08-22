"""驗收：SSE 的斷線續傳、中止與 G-06（09 §3.2、06 §4、13 §3 工作包 1D-4b）。

1D-4a 把線接通了，但**只在一切順利時**：client 從頭讀到尾、不斷線、不按停止、行程不
重啟。這一包補的是剩下那些情況，而它們在真實網路上不是例外而是常態。

13 對 1D 的技術重點寫得很明白：「SSE 協定完整度（不留技術債，**resume day-1 做齊**）」。
理由是它一旦後補就補不乾淨——事件編號、心跳、緩衝區生命週期會先被寫成「不需要 resume」
的形狀，而那些假設散在三個檔案裡。

四件事錯了都不會有例外：

1. **續傳漏一段**。重連時多給或少給一個編號，使用者看到的是重複的字或憑空少一句，
   而它只在「剛好斷在中間」時出現。
2. **斷線就把生成丟掉**。token 的錢在斷線那一刻已經花掉了（06 §4 的 G-06），丟掉等於
   付了錢卻沒有東西可以給使用者看——而下次重整只會看到一則永遠停在 streaming 的訊息。
3. **停止只停得了自己那一台**。正式環境是每 replica 兩個 worker × N replica（11 §45），
   停止請求幾乎不會落回產生它的那個行程。停不掉的話，使用者按了停止而帳單繼續跑。
4. **重啟把進行中的生成蒸發掉**。11 §196 的 graceful shutdown 要求送出
   `error(retryable)` 再退出，否則使用者的畫面會永遠停在半句話。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing
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


@pytest.fixture
def conversation() -> uuid.UUID:
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


async def _token(client: httpx.AsyncClient, email: str = OWNER_EMAIL) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": SLUG_A, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}


async def _start(client: httpx.AsyncClient, conversation_id: uuid.UUID, token: str) -> str:
    created = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"content": "年假幾天？"},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["message_id"])


def _stream_url(conversation_id: uuid.UUID, message_id: str) -> str:
    return f"/api/v1/conversations/{conversation_id}/messages/{message_id}/stream"


async def _read(
    client: httpx.AsyncClient,
    conversation_id: uuid.UUID,
    message_id: str,
    token: str,
    *,
    last_event_id: str | None = None,
    stop_after: int | None = None,
) -> list[tuple[int, str, dict[str, Any]]]:
    """讀串流，回傳 [(event id, 事件名, payload)]。

    `stop_after` 是「讀到第 N 個事件就把連線扯掉」——模擬使用者關掉分頁。
    """
    headers = dict(_auth(token))
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id

    events: list[tuple[int, str, dict[str, Any]]] = []
    async with client.stream(
        "GET", _stream_url(conversation_id, message_id), headers=headers
    ) as response:
        assert response.status_code == 200, await response.aread()
        seq, name = 0, ""
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                seq = int(line.removeprefix("id: "))
            elif line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                events.append((seq, name, json.loads(line.removeprefix("data: "))))
                if stop_after is not None and len(events) >= stop_after:
                    break
    return events


def _text_of(events: list[tuple[int, str, dict[str, Any]]]) -> str:
    return "".join(payload["text"] for _, name, payload in events if name == "delta")


async def _message(message_id: str) -> Any:
    return await run_orm(_load_message, uuid.UUID(message_id))


def _load_message(message_id: uuid.UUID) -> Any:
    from apps.conversation.models import Message

    with tenant_scope(TENANT_A):
        return Message.objects.get(id=message_id)


class TestResume:
    async def test_without_the_header_it_starts_from_the_beginning(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """沒帶 `Last-Event-ID` = 第一次連上，從第 1 號開始。"""
        token = await _token(client)
        message_id = await _start(client, conversation, token)

        events = await _read(client, conversation, message_id, token)

        assert events[0][0] == 1

    async def test_it_continues_after_the_last_delivered_event(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """**這是本檔的核心。** 重連只拿沒收到過的那些——不重、不漏。

        多給一個編號使用者會看到重複的字；少給一個會憑空少一句。兩者都只在「剛好斷在
        中間」時出現，所以它們幾乎不會在開發期被看到。
        """
        token = await _token(client)
        message_id = await _start(client, conversation, token)

        first = await _read(client, conversation, message_id, token, stop_after=3)
        resumed = await _read(
            client, conversation, message_id, token, last_event_id=str(first[-1][0])
        )

        assert resumed[0][0] == first[-1][0] + 1, "續傳要從下一個編號開始"
        assert [seq for seq, _, _ in first + resumed] == list(
            range(1, len(first) + len(resumed) + 1)
        ), "編號要連續，不重不漏"

    async def test_the_reassembled_answer_matches_what_was_stored(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """斷過一次之後拼起來的字，要等於資料庫裡的那一份。

        這條比「編號連續」更貼近使用者：編號對而內容錯（例如續傳時漏掉某個 delta 的
        payload）在畫面上就是少一段話。
        """
        token = await _token(client)
        message_id = await _start(client, conversation, token)

        first = await _read(client, conversation, message_id, token, stop_after=2)
        resumed = await _read(
            client, conversation, message_id, token, last_event_id=str(first[-1][0])
        )

        stored = await _message(message_id)
        assert _text_of(first + resumed) == stored.content

    async def test_resuming_a_finished_stream_replays_the_tail(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """生成早就結束了才重連——緩衝區還在（TTL 5 分鐘），所以補得完。

        這是最常見的情境：使用者切走再切回來，而那通常只有幾秒。
        """
        token = await _token(client)
        message_id = await _start(client, conversation, token)
        full = await _read(client, conversation, message_id, token)

        resumed = await _read(client, conversation, message_id, token, last_event_id="1")

        assert [seq for seq, _, _ in resumed] == [seq for seq, _, _ in full[1:]]

    async def test_an_expired_buffer_is_a_conflict(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """緩衝區過期 → `409 RESUME_EXPIRED`（09 §3.2、附錄 A）。

        **不能回 200 + 空串流**：那對 client 是「這則訊息沒有內容」，而它其實有——
        只是要改用 `GET /messages` 去抓最終結果。409 帶的正是這個指示。
        """
        token = await _token(client)
        message_id = await _start(client, conversation, token)
        await _read(client, conversation, message_id, token)
        get_redis().delete(tenant_key(TENANT_A, "sse", message_id))  # 模擬 TTL 到期

        response = await client.get(
            _stream_url(conversation, message_id),
            headers={**_auth(token), "Last-Event-ID": "2"},
        )

        assert response.status_code == 409
        assert response.json()["code"] == "RESUME_EXPIRED"

    async def test_a_malformed_header_is_rejected(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """壞掉的 `Last-Event-ID` 回 422，**不當成從頭開始**（同 1D-2 對游標的決定）。

        當成 0 的話，一個編號寫錯的 client 會每次重連都從頭收一遍，而畫面上是「回答
        一直重複」——沒有錯誤，只有怪現象。
        """
        token = await _token(client)
        message_id = await _start(client, conversation, token)

        response = await client.get(
            _stream_url(conversation, message_id),
            # ASCII：HTTP header 不能帶非 latin-1 的位元組，真的 client 也送不出中文。
            headers={**_auth(token), "Last-Event-ID": "not-a-number"},
        )

        assert response.status_code == 422


class TestClientDisconnect:
    async def test_generation_survives_a_disconnect(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """**G-06**（06 §4）：client 斷線 → 伺服器繼續收完 → 完整持久化。

        token 的錢在斷線那一刻已經花掉了。丟掉等於付了錢卻沒有東西給使用者看，而下次
        重整只會看到一則永遠停在 streaming 的訊息。
        """
        token = await _token(client)
        message_id = await _start(client, conversation, token)

        await _read(client, conversation, message_id, token, stop_after=2)  # 讀兩個就走
        from api.background import drain

        await drain(timeout_seconds=10)

        stored = await _message(message_id)
        assert stored.status == "completed"
        assert stored.content, "斷線之後產生的內容也要留下來"

    async def test_the_buffer_still_holds_the_whole_answer(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """斷線之後產生的事件仍要進緩衝區——否則重連時補不回中間那一段。"""
        token = await _token(client)
        message_id = await _start(client, conversation, token)

        await _read(client, conversation, message_id, token, stop_after=2)
        from api.background import drain

        await drain(timeout_seconds=10)
        resumed = await _read(client, conversation, message_id, token, last_event_id="2")

        assert [name for _, name, _ in resumed][-1] == "done"


class TestStop:
    async def test_it_accepts_the_request(
        self, client: httpx.AsyncClient, conversation: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """09 §2.4 的「中止生成」。202：中止是**非同步**的——真正停下來的是另一個
        行程裡的那個 task（09 §3.3 的 202 慣例）。"""
        _slow_provider(monkeypatch)
        token = await _token(client)
        message_id = await _start(client, conversation, token)

        response = await client.post(
            f"/api/v1/conversations/{conversation}/messages/{message_id}/stop",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 202

    async def test_it_writes_a_flag_that_another_process_can_see(
        self, client: httpx.AsyncClient, conversation: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**中止必須跨行程**（11 §45：每 replica 兩個 worker × N replica）。

        停止請求幾乎不會落回產生那則訊息的行程，所以「在記憶體裡設一個旗標」是錯的
        ——它只停得了剛好接到請求的那一台，而使用者按了停止、帳單繼續跑。這條測試
        直接盯住那個旗標在 Redis 裡（並且帶租戶前綴，鐵則 4）。
        """
        _slow_provider(monkeypatch)
        token = await _token(client)
        message_id = await _start(client, conversation, token)

        await client.post(
            f"/api/v1/conversations/{conversation}/messages/{message_id}/stop",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert get_redis().exists(tenant_key(TENANT_A, "sse", message_id, "stop"))

    async def test_the_generation_stops_and_keeps_what_it_had(
        self, client: httpx.AsyncClient, conversation: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """已經產生的部分要留著：使用者按停止是因為**看到的內容已經夠了**，不是想
        把它丟掉（05 §3.4 的 `interrupted`）。"""
        _slow_provider(monkeypatch)
        token = await _token(client)
        message_id = await _start(client, conversation, token)
        await asyncio.sleep(0.15)  # 讓它先產生一些

        await client.post(
            f"/api/v1/conversations/{conversation}/messages/{message_id}/stop",
            headers={"Authorization": f"Bearer {token}"},
        )
        from api.background import drain

        await drain(timeout_seconds=10)

        stored = await _message(message_id)
        assert stored.status == "interrupted"
        assert stored.content, "停止前已經產生的內容不該被丟掉"

    async def test_the_stream_ends_with_done_not_an_error(
        self, client: httpx.AsyncClient, conversation: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**使用者自己按的停止不是錯誤。**

        送 `error` 的話，前端會照 09 §1.3 的慣例顯示一個紅色的失敗訊息——而使用者剛剛
        得到的正是他要的結果。`finish_reason` 記 `stopped`，讓前端分得出「講完了」與
        「被停下來」。
        """
        _slow_provider(monkeypatch)
        token = await _token(client)
        message_id = await _start(client, conversation, token)
        await asyncio.sleep(0.15)

        await client.post(
            f"/api/v1/conversations/{conversation}/messages/{message_id}/stop",
            headers={"Authorization": f"Bearer {token}"},
        )
        events = await _read(client, conversation, message_id, token)

        assert events[-1][1] == "done"
        assert events[-1][2]["finish_reason"] == "stopped"

    async def test_stopping_a_finished_generation_changes_nothing(
        self, client: httpx.AsyncClient, conversation: uuid.UUID
    ) -> None:
        """使用者在最後一個 token 到達的同一瞬間按下停止——這是他躲不掉的競態，
        不該因此看到錯誤，也不該把一則已經完成的回答改成中斷。"""
        token = await _token(client)
        message_id = await _start(client, conversation, token)
        await _read(client, conversation, message_id, token)

        response = await client.post(
            f"/api/v1/conversations/{conversation}/messages/{message_id}/stop",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 202
        assert (await _message(message_id)).status == "completed"

    async def test_another_user_cannot_stop_it(
        self, client: httpx.AsyncClient, conversation: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """擁有者制（1D-2）：`message_id` 會出現在網址與 log 裡，而中止別人的生成
        是一個免費又難以察覺的破壞。"""
        _slow_provider(monkeypatch)
        token = await _token(client)
        message_id = await _start(client, conversation, token)
        intruder = await _token(client, email=OTHER_EMAIL)

        response = await client.post(
            f"/api/v1/conversations/{conversation}/messages/{message_id}/stop",
            headers={"Authorization": f"Bearer {intruder}"},
        )

        assert response.status_code == 404


class TestHeartbeat:
    async def test_a_quiet_stream_still_sends_something(
        self, client: httpx.AsyncClient, conversation: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """久久沒有事件時要送 `: heartbeat`（09 §3.2）。

        不送的話，中間的 proxy 會把一條「看起來沒在用」的連線剪斷（12 §56），而使用者
        看到的是回答到一半突然停住——而且只在**慢的**那些回答上發生。

        這裡把間隔調小（正式值 15 秒），否則這條測試要跑 15 秒。
        """
        monkeypatch.setattr("api.v1.conversations.HEARTBEAT_SECONDS", 0.05)
        _slow_provider(monkeypatch, delay=0.2)
        token = await _token(client)
        message_id = await _start(client, conversation, token)

        chunks: list[str] = []
        async with client.stream(
            "GET", _stream_url(conversation, message_id), headers=_auth(token)
        ) as response:
            async for line in response.aiter_lines():
                chunks.append(line)
                if len(chunks) > 12:
                    break

        assert any(line.startswith(":") for line in chunks), "沒有心跳"


def _slow_provider(monkeypatch: pytest.MonkeyPatch, *, delay: float = 0.05) -> None:
    """一個吐得很慢的 provider——中止與心跳都需要「生成還在跑」的那段時間。"""
    from collections.abc import AsyncGenerator

    from ai.gateway.chat import ChatRequest, ChatTimeouts, DoneDelta, ProviderDelta, TextDelta
    from api.v1 import conversations as endpoint

    class _SlowProvider:
        name = "slow"

        async def stream_chat(
            self, request: ChatRequest, *, timeouts: ChatTimeouts
        ) -> AsyncGenerator[ProviderDelta, None]:
            for index in range(20):
                await asyncio.sleep(delay)
                yield TextDelta(text=f"第{index}段。")
            yield DoneDelta(finish_reason="stop")

    monkeypatch.setattr("ai.gateway._chat_provider", lambda name: _SlowProvider(), raising=True)
    # `ChatService` 會快取 Gateway（惰性建立），而端點模組的 `_chat` 是 module 層單例
    # ——前一條測試早就把它建好了，只 patch 解析函式的話這裡換的東西沒有人會再讀。
    monkeypatch.setattr(endpoint._chat, "_gateway", None, raising=False)


class TestVanishedBuffer:
    """產生那則訊息的行程**硬崩潰**（OOM、kill -9）時，讀取端要收得了尾。

    優雅關機有 `ChatService` 的 shield 收尾，硬殺沒有：終局事件永遠不會寫進來，而緩衝區
    5 分鐘後過期。原本的迴圈只在收到 `done`／`error` 時結束，於是它會**永遠**送心跳
    ——前端一直顯示「正在輸入」，一條連線一直掛著。DB 那一列由補償掃描標成 interrupted
    （2026-08-22 加的），但那救不到已經連上的讀取端：它改得動 DB，改不動一個已經不
    存在的緩衝區。
    """

    async def test_it_ends_with_an_error_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from api.v1.conversations import _sse
        from core.streams import StreamBuffer

        monkeypatch.setattr("api.v1.conversations.HEARTBEAT_SECONDS", 0.05)
        message_id = uuid.uuid4()
        buffer = StreamBuffer(tenant_id=TENANT_A, message_id=message_id)
        await buffer.append("delta", {"text": "答到一半"})

        frames: list[str] = []
        async with asyncio.timeout(10):
            async for frame in _sse(TENANT_A, message_id):
                frames.append(frame)
                if len(frames) == 1:
                    # 產生端消失，緩衝區隨 TTL 過期。
                    await buffer.drop()

        assert "STREAM_INTERRUPTED" in frames[-1]
        assert frames[-1].startswith("event: error") or "event: error" in frames[-1]

    async def test_the_partial_answer_is_delivered_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """已經寫進緩衝區的內容要先送完——收尾不是「把畫面清掉」。"""
        from api.v1.conversations import _sse
        from core.streams import StreamBuffer

        monkeypatch.setattr("api.v1.conversations.HEARTBEAT_SECONDS", 0.05)
        message_id = uuid.uuid4()
        buffer = StreamBuffer(tenant_id=TENANT_A, message_id=message_id)
        await buffer.append("delta", {"text": "第一段"})

        frames: list[str] = []
        async with asyncio.timeout(10):
            async for frame in _sse(TENANT_A, message_id):
                frames.append(frame)
                if len(frames) == 1:
                    await buffer.drop()

        assert "第一段" in frames[0]

    async def test_a_live_buffer_keeps_heartbeating(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """**緩衝區還在就不准放棄。** 判定寫得太急的話，一個回答很慢的模型會讓使用者
        看到「生成已中斷」，而它其實還在跑。"""
        from api.v1.conversations import _sse
        from core.streams import StreamBuffer

        monkeypatch.setattr("api.v1.conversations.HEARTBEAT_SECONDS", 0.05)
        message_id = uuid.uuid4()
        buffer = StreamBuffer(tenant_id=TENANT_A, message_id=message_id)
        await buffer.append("delta", {"text": "慢慢想"})

        frames: list[str] = []
        # `aclosing`：提前 break 的話 generator 要被關掉，否則它會留到 GC 才收
        # （同 ai/gateway 對每一層 async generator 的處置）。
        async with aclosing(_sse(TENANT_A, message_id)) as stream, asyncio.timeout(10):
            async for frame in stream:
                frames.append(frame)
                if len(frames) >= 6:
                    break

        assert all("STREAM_INTERRUPTED" not in frame for frame in frames[1:])
        assert any(frame.startswith(":") for frame in frames), "沒有心跳"

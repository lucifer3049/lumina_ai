"""驗收：stop 與收尾故障下的計費正確性（2026-08-30 深度審查的 chat.py 三條）。

三件事錯了都不會有例外，而且帳面看起來更漂亮：

1. **stop 不記帳**。UsageDelta 只在串流自然走完時送出，按停止的回合 usage 是空的
   ——不記 usage_logs、token 預留整筆退回。但 provider 按產出計價（地端則是 GPU
   真的燒了那段時間）：一個腳本反覆「長問題＋快講完時 stop」，月度 token 配額
   形同虛設，成本對帳永遠短報。gateway 對**斷線**已會補估算（`_StreamState.
   estimated_usage`），唯獨 stop 是我們主動棄讀，補的那一筆永遠到不了——所以
   估算要在 `ChatService` 這一側做。
2. **stop 之後上游還在燒**。generator 不關，provider 的 HTTP 連線與生成要等 GC
   才停；收尾的 DB＋Redis 往返期間，每一毫秒都還在計費／佔 GPU。
3. **收尾故障把完成的回合改寫成失敗**。`_complete` 先 settle 再做 Redis appends，
   後者一炸就落進 `_fail`：已持久化為 completed 的訊息被改寫成 failed，而
   `QuotaService.commit` 是非冪等的 incrby——校正量被套用兩遍。

走真的端點（分工同 test_quota_chat.py：計數器語意在 integration，這裡驗接上
之後的行為）。LLM 一律 MockProvider（CLAUDE.md）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from ai.gateway.chat import (
    ChatRequest,
    ChatTimeouts,
    DoneDelta,
    ProviderDelta,
    TextDelta,
    UsageDelta,
)
from ai.gateway.providers.mock import MockChatProvider
from api.main import create_app
from common.passwords import hash_password
from core.db import run_orm
from core.redis import get_redis, tenant_key
from core.streams import StreamBuffer
from tests.conftest import TENANT_A
from tests.factories.conversation import make_conversation
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.seed import ensure_identity_seed, ensure_prompt_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG = "tenant-a"
OWNER_EMAIL = "owner@example.com"

_PROMPT_TOKENS = 10
_COMPLETION_TOKENS = 5


@pytest.fixture(autouse=True)
async def _drain_background_generation() -> AsyncIterator[None]:
    yield
    from api.background import drain

    await drain(timeout_seconds=10)


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    keys = list(client.scan_iter(match=tenant_key(TENANT_A, "*")))
    if keys:
        client.delete(*keys)


@pytest.fixture
def journal(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """吐不完的 provider ＋ 順序日誌。

    「吐不完」是 stop 測試的前提：中止旗標只在 delta 抵達時輪詢（0.2s 節流），
    有限腳本會在停止生效前自己講完。generator 被關（GeneratorExit）時在 finally
    記一筆——那就是「上游真的停了」的時點。
    """
    log: dict[str, Any] = {"sequence": []}

    async def stream_chat(
        self: Any, request: ChatRequest, *, timeouts: ChatTimeouts
    ) -> AsyncIterator[ProviderDelta]:
        try:
            while True:
                yield TextDelta(text="地端模型還在產出，")
                await asyncio.sleep(0.01)
        finally:
            log["sequence"].append("provider_closed")

    monkeypatch.setattr(MockChatProvider, "stream_chat", stream_chat)
    return log


def _finite_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    async def stream_chat(
        self: Any, request: ChatRequest, *, timeouts: ChatTimeouts
    ) -> AsyncIterator[ProviderDelta]:
        yield TextDelta(text="（測試）回答。")
        yield UsageDelta(
            prompt_tokens=_PROMPT_TOKENS,
            completion_tokens=_COMPLETION_TOKENS,
            model=request.model,
        )
        yield DoneDelta(finish_reason="stop")

    monkeypatch.setattr(MockChatProvider, "stream_chat", stream_chat)


def _owner() -> uuid.UUID:
    from apps.identity.models import Role

    ensure_identity_seed()
    ensure_prompt_seed()
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG, settings={"quota": {"tokens_month": 100_000}})
        user = make_user(
            tenant_id=TENANT_A, email=OWNER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=user, role=Role.objects.get(tenant__isnull=True, name="owner"))
    return uuid.UUID(str(user.id))


def _conversation(owner_id: uuid.UUID) -> uuid.UUID:
    with tenant_scope(TENANT_A):
        conversation = make_conversation(tenant_id=TENANT_A, user_id=owner_id, kb_ids=[])
    return uuid.UUID(str(conversation.id))


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": SLUG, "email": OWNER_EMAIL, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _start(client: httpx.AsyncClient, conversation_id: uuid.UUID, token: str) -> str:
    created = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=_auth(token),
        json={"content": "測試問題"},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["message_id"])


async def _stop(
    client: httpx.AsyncClient, conversation_id: uuid.UUID, message_id: str, token: str
) -> None:
    stopped = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/{message_id}/stop",
        headers=_auth(token),
    )
    assert stopped.status_code == 202, stopped.text


async def _drain() -> None:
    from api.background import drain

    await drain(timeout_seconds=10)


async def _quota_used(client: httpx.AsyncClient, token: str, resource: str) -> int:
    response = await client.get("/api/v1/tenants/current/quota", headers=_auth(token))
    assert response.status_code == 200, response.text
    items = {item["resource"]: item for item in response.json()["items"]}
    return int(items[resource]["used"])


def _llm_rows() -> list[Any]:
    from apps.platform.models import UsageLog

    with tenant_scope(TENANT_A):
        return list(UsageLog.objects.filter(category="llm").order_by("created_at"))


def _message_status(message_id: str) -> str:
    from apps.conversation.models import Message

    with tenant_scope(TENANT_A):
        return str(Message.objects.get(id=uuid.UUID(message_id)).status)


async def _run_stopped_turn(
    client: httpx.AsyncClient, journal: dict[str, Any]
) -> tuple[str, str]:
    owner = await run_orm(_owner)
    conversation = await run_orm(_conversation, owner)
    token = await _token(client)
    message_id = await _start(client, conversation, token)
    await asyncio.sleep(0.05)  # 先產出幾段，確保停在分水嶺之後
    await _stop(client, conversation, message_id, token)
    await _drain()
    return message_id, token


class TestStopSettlesTheBill:
    async def test_a_stopped_turn_lands_an_estimated_usage_row(
        self, client: httpx.AsyncClient, journal: dict[str, Any]
    ) -> None:
        """按停止的回合也要有一筆帳：token 已經產生費用，與有沒有講完無關。"""
        message_id, _ = await _run_stopped_turn(client, journal)

        rows = await run_orm(_llm_rows)

        assert len(rows) == 1, "stop 的回合必須留下一筆 usage_logs"
        assert rows[0].request_id == message_id
        assert rows[0].completion_tokens > 0, "已產出的段落要反映在帳上"

    async def test_the_token_quota_is_committed_not_refunded(
        self, client: httpx.AsyncClient, journal: dict[str, Any]
    ) -> None:
        """tokens_month 收在估算值：整筆退回的話，反覆 stop 就能無限用量。"""
        _, token = await _run_stopped_turn(client, journal)

        used = await _quota_used(client, token, "tokens_month")

        assert used > 0, "stop 不得把 token 預留整筆退回"

    async def test_the_message_still_ends_interrupted(
        self, client: httpx.AsyncClient, journal: dict[str, Any]
    ) -> None:
        """記帳不改變 stop 的語意：使用者按的停止不是錯誤（05 §3.4）。"""
        message_id, _ = await _run_stopped_turn(client, journal)

        assert await run_orm(_message_status, message_id) == "interrupted"

    async def test_the_upstream_generator_closes_before_the_wrap_up(
        self, client: httpx.AsyncClient, journal: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """先關上游、再收尾：收尾要做 DB 與多趟 Redis 往返，晚關的每一毫秒
        provider 都還在計費（地端則是 GPU 還在替沒有人要的回答產 token）。"""
        original_settle = StreamBuffer.settle

        async def recording_settle(self: StreamBuffer) -> None:
            journal["sequence"].append("settled")
            await original_settle(self)

        monkeypatch.setattr(StreamBuffer, "settle", recording_settle)

        await _run_stopped_turn(client, journal)

        sequence = journal["sequence"]
        assert "provider_closed" in sequence and "settled" in sequence, sequence
        assert sequence.index("provider_closed") < sequence.index("settled"), (
            f"上游要在收尾前關掉，實際順序：{sequence}"
        )


class TestFinalizeIsAOneWayValve:
    async def test_a_done_append_failure_does_not_refail_or_double_settle(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """訊息持久化為 completed 之後，Redis 抖動不得把它改寫成 failed，
        也不得讓 `commit` 的校正量套用兩遍（incrby 非冪等）。"""
        _finite_provider(monkeypatch)
        original_append = StreamBuffer.append

        async def flaky_append(self: StreamBuffer, event: str, data: dict[str, Any]) -> None:
            if event == "done":
                raise ConnectionError("redis 在 done 那一刻抖了一下")
            await original_append(self, event, data)

        monkeypatch.setattr(StreamBuffer, "append", flaky_append)

        owner = await run_orm(_owner)
        conversation = await run_orm(_conversation, owner)
        token = await _token(client)
        message_id = await _start(client, conversation, token)
        await _drain()

        assert await run_orm(_message_status, message_id) == "completed", (
            "完整生成並持久化的回答不得因收尾的 Redis 故障被改寫成 failed"
        )
        used = await _quota_used(client, token, "tokens_month")
        assert used == _PROMPT_TOKENS + _COMPLETION_TOKENS, (
            f"tokens_month 應收在實際的 {_PROMPT_TOKENS + _COMPLETION_TOKENS}，實際 {used}"
            "——不相等代表 settle 被跑了兩遍（double-settle）或整筆退回"
        )
        rows = await run_orm(_llm_rows)
        assert len(rows) == 1

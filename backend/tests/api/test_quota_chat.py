"""驗收：chat 路徑的配額強制執行（04 §8.1、09 §1.3 的 429，2A-2a）。

Phase 2 的 DoD 就是這一條：「雙租戶隔離下 quota 強制生效（超額被擋）」（13 §4）。
擋線畫在 `POST /messages`——**在建立任何訊息之前**：擋在生成開始之後的話，
錢已經花了，擋的只是畫面。

走真的端點（分工同 test_usage_recording.py：計數器語意在
tests/integration/test_quota_counters.py，這裡驗接上之後的行為）。

三件事錯了都不會有例外：

1. **被擋的請求留下半個回合**。429 了但 user 訊息已建立——畫面上有一個永遠
   不會有回答的問題，且它還占了一則訊息額度。
2. **token 按估計值入帳**。每一輪都扣 2000 而不是實際的十幾，月中額度就見底，
   而 usage_logs 的帳完全對不上計數器。
3. **串流結束沒有歸還並發位**。gauge 只加不減，第 N 輪之後這個租戶永遠 429，
   重啟 Redis 才會好——而那看起來像「配額壞了」，不是「洩漏」。
"""

from __future__ import annotations

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


@pytest.fixture(autouse=True)
def _scripted_provider(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _owner_with_quota(quota: dict[str, Any]) -> uuid.UUID:
    from apps.identity.models import Role

    ensure_identity_seed()
    ensure_prompt_seed()
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG, settings={"quota": quota})
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


async def _post_message(
    client: httpx.AsyncClient, conversation_id: uuid.UUID, token: str
) -> httpx.Response:
    return await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=_auth(token),
        json={"content": "測試問題"},
    )


async def _run_turn(client: httpx.AsyncClient, conversation_id: uuid.UUID, token: str) -> None:
    """跑完整輪：送出問題、把串流讀到伺服器收尾、等背景 task 收工。"""
    created = await _post_message(client, conversation_id, token)
    assert created.status_code == 201, created.text
    events: list[str] = []
    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/messages/{created.json()['message_id']}/stream",
        headers={**_auth(token), "Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, await response.aread()
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                events.append(line.removeprefix("event: "))
    assert events and events[-1] == "done", events

    from api.background import drain

    await drain(timeout_seconds=10)


async def _quota_status(client: httpx.AsyncClient, token: str) -> dict[str, dict[str, Any]]:
    response = await client.get("/api/v1/tenants/current/quota", headers=_auth(token))
    assert response.status_code == 200, response.text
    return {item["resource"]: item for item in response.json()["items"]}


async def _message_count(client: httpx.AsyncClient, conversation_id: uuid.UUID, token: str) -> int:
    listed = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=_auth(token)
    )
    assert listed.status_code == 200, listed.text
    return len(listed.json()["items"])


class TestMessagesPerDay:
    async def test_the_limit_blocks_with_429_and_no_half_turn(
        self, client: httpx.AsyncClient
    ) -> None:
        owner = await run_orm(_owner_with_quota, {"messages_day": 1})
        conversation = await run_orm(_conversation, owner)
        token = await _token(client)
        await _run_turn(client, conversation, token)
        before = await _message_count(client, conversation, token)

        blocked = await _post_message(client, conversation, token)

        assert blocked.status_code == 429, blocked.text
        body = blocked.json()
        assert body["code"] == "QUOTA_EXCEEDED"
        assert body["details"]["resource"] == "messages_day"
        assert await _message_count(client, conversation, token) == before, (
            "被擋的請求不得留下任何訊息（半個回合）"
        )


class TestTokensPerMonth:
    async def test_an_exhausted_budget_blocks_the_turn(self, client: httpx.AsyncClient) -> None:
        """上限低於預留估計值（2000）＝額度見底——開場就該擋，而不是生成完才發現。"""
        owner = await run_orm(_owner_with_quota, {"tokens_month": 100})
        conversation = await run_orm(_conversation, owner)
        token = await _token(client)

        blocked = await _post_message(client, conversation, token)

        assert blocked.status_code == 429, blocked.text
        assert blocked.json()["details"]["resource"] == "tokens_month"

    async def test_the_committed_amount_is_the_actual_usage(
        self, client: httpx.AsyncClient
    ) -> None:
        """一輪之後計數器是實際的 15，不是預留的 2000（commit 校正，04 §8.1）。"""
        owner = await run_orm(_owner_with_quota, {"tokens_month": 100_000})
        conversation = await run_orm(_conversation, owner)
        token = await _token(client)

        await _run_turn(client, conversation, token)

        status = await _quota_status(client, token)
        assert status["tokens_month"]["used"] == _PROMPT_TOKENS + _COMPLETION_TOKENS


class TestConcurrentStreams:
    async def test_a_full_gauge_blocks_new_turns(self, client: httpx.AsyncClient) -> None:
        owner = await run_orm(_owner_with_quota, {"streams": 0})
        conversation = await run_orm(_conversation, owner)
        token = await _token(client)

        blocked = await _post_message(client, conversation, token)

        assert blocked.status_code == 429, blocked.text
        assert blocked.json()["details"]["resource"] == "streams"

    async def test_a_finished_turn_releases_its_slot(self, client: httpx.AsyncClient) -> None:
        """跑完一輪 gauge 回 0——只加不減的話，第 N+1 輪起永遠 429（洩漏，
        不是配額）。"""
        owner = await run_orm(_owner_with_quota, {"streams": 1})
        conversation = await run_orm(_conversation, owner)
        token = await _token(client)

        await _run_turn(client, conversation, token)
        status = await _quota_status(client, token)
        assert status["streams"]["used"] == 0

        again = await _post_message(client, conversation, token)
        assert again.status_code == 201, "上一輪結束後，並發位必須空出來"

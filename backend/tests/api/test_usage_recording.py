"""驗收：chat 回合的 usage 落地（04 §8.2、09 §3.2 的 `usage` 事件，13 §4 工作包 2A-1）。

`usage` SSE 事件（1D-4a）讓**前端**看得到這一輪花了什麼；本包讓**帳**也看得到：
每一輪生成在 usage_logs 恰一列。Quota（2A-2）按它扣、Analytics（2A-3）按它加總，
兩邊共用同一列——各記各的一定對不上。

走真的端點、真的背景生成（分工同 test_chat_citations.py：純邏輯在 unit，
這裡驗接起來之後的行為）。

三件事錯了都不會有例外：

1. **一輪多列或零列**。多列＝重複計費，零列＝漏帳；兩者的畫面都完全正常。
2. **request_id 對不回 message**。對帳查「這一筆是哪一次呼叫」是稽核的基本問題，
   斷了就只剩時間戳可猜。
3. **落地失敗殺掉回答**。usage 是旁路（unit 層已釘 service 不拋），但接線若把
   record 放在錯的位置（例如 persist 之前、或在同一個交易裡），DB 抖一下時
   使用者看到的是回答消失——而 usage 本來就沒記成，等於雙輸。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from services.platform.pricing import compute_cost

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
from apps.platform.models import UsageLog
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
    """固定 token 數的假模型——cost 的斷言需要已知的輸入。"""

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


@pytest.fixture
def owner() -> uuid.UUID:
    from apps.identity.models import Role

    ensure_identity_seed()
    ensure_prompt_seed()
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG)
        user = make_user(
            tenant_id=TENANT_A, email=OWNER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=user, role=Role.objects.get(tenant__isnull=True, name="owner"))
    return uuid.UUID(str(user.id))


@pytest.fixture
def chat(owner: uuid.UUID) -> uuid.UUID:
    with tenant_scope(TENANT_A):
        conversation = make_conversation(tenant_id=TENANT_A, user_id=owner, kb_ids=[])
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


async def _ask(
    client: httpx.AsyncClient, conversation_id: uuid.UUID, token: str
) -> tuple[uuid.UUID, list[tuple[str, dict[str, Any]]]]:
    headers = {"Authorization": f"Bearer {token}"}
    created = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers,
        json={"content": "測試問題"},
    )
    assert created.status_code == 201, created.text
    message_id = uuid.UUID(created.json()["message_id"])

    events: list[tuple[str, dict[str, Any]]] = []
    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/messages/{message_id}/stream",
        headers={**headers, "Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, await response.aread()
        name = ""
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                events.append((name, json.loads(line.removeprefix("data: "))))
    return message_id, events


def _llm_rows() -> list[UsageLog]:
    with tenant_scope(TENANT_A):
        return list(UsageLog.objects.filter(category="llm").order_by("created_at"))


class TestChatUsageRecording:
    async def test_a_completed_turn_lands_exactly_one_row(
        self, client: httpx.AsyncClient, chat: uuid.UUID, owner: uuid.UUID
    ) -> None:
        token = await _token(client)
        message_id, events = await _ask(client, chat, token)
        assert events[-1][0] == "done", events[-1]

        rows = await run_orm(_llm_rows)

        assert len(rows) == 1
        row = rows[0]
        assert row.prompt_tokens == _PROMPT_TOKENS
        assert row.completion_tokens == _COMPLETION_TOKENS
        assert row.request_id == str(message_id), "request_id 必須對映得回這一輪的 message"
        assert row.conversation_id == chat
        assert row.user_id == owner, "誰花的錢要記在誰名下（Analytics 按 user 分組）"

    async def test_the_model_and_cost_match_the_usage_event(
        self, client: httpx.AsyncClient, chat: uuid.UUID
    ) -> None:
        """落地的 model 與串流 `meta` 事件的 model 一致（同一輪不能兩套說法），
        cost 是按價目表算出來的值——不是 None、不是 0 佔位。"""
        token = await _token(client)
        _, events = await _ask(client, chat, token)
        meta = next(data for name, data in events if name == "meta")

        rows = await run_orm(_llm_rows)

        assert rows[0].model == meta["model"]
        assert rows[0].cost == compute_cost(
            str(meta["model"]),
            prompt_tokens=_PROMPT_TOKENS,
            completion_tokens=_COMPLETION_TOKENS,
        )
        assert rows[0].cost is not None

    async def test_two_turns_land_two_rows(
        self, client: httpx.AsyncClient, chat: uuid.UUID
    ) -> None:
        token = await _token(client)
        first_id, _ = await _ask(client, chat, token)
        second_id, _ = await _ask(client, chat, token)

        rows = await run_orm(_llm_rows)

        assert [row.request_id for row in rows] == [str(first_id), str(second_id)]

    async def test_a_broken_usage_repository_does_not_break_the_answer(
        self,
        client: httpx.AsyncClient,
        chat: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """旁路故障：usage 落地掛掉，串流照樣走到 `done`、訊息照樣 completed。
        反過來（回答跟著消失）是雙輸——帳沒記成，使用者的東西也沒了。"""
        from repositories.platform import UsageLogRepository

        def exploding_add(self: Any, **fields: Any) -> None:
            raise RuntimeError("db is down")

        monkeypatch.setattr(UsageLogRepository, "add", exploding_add)

        token = await _token(client)
        message_id, events = await _ask(client, chat, token)

        assert events[-1][0] == "done", f"usage 落地失敗不該影響串流：{events[-1]}"
        listed = await client.get(
            f"/api/v1/conversations/{chat}/messages",
            headers={"Authorization": f"Bearer {token}"},
        )
        message = listed.json()["items"][-1]
        assert message["id"] == str(message_id)
        assert message["status"] == "completed"
        assert await run_orm(_llm_rows) == []

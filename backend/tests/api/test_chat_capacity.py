"""驗收：行程額滿時的 429，**以及它擋在哪一步**（二次架構審計 F-04）。

行為本身在 `tests/unit/test_generation_capacity.py`；這裡驗的是接上端點之後的兩件事，
而它們都只有從外面看才看得出來：

1. **擋在建立回合之前**。擋在後面的話，被拒的請求已經寫了兩則訊息、扣了三種額度，
   而使用者拿到的是 429——那比不擋更糟：畫面上多一個永遠不會有回答的問題，而且
   它還吃掉了額度。這正是 `test_quota_chat.py` 對配額擋線的同一條要求。
2. **429 要帶 `Retry-After`**。不帶的話 client 只能自己猜，而猜出來的多半是
   「立刻重試」——正好在系統最擠的時候再加一份負載。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from api.background import spawn
from api.main import create_app
from common.passwords import hash_password
from config.settings.app_settings import get_app_settings
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


@pytest.fixture(autouse=True)
def _capacity_of_zero_slots(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把上限壓到 1，測試自己佔掉那一個名額——比起真的開 64 條生成快得多，
    而驗到的是同一條判斷。"""
    monkeypatch.setenv("API_MAX_CONCURRENT_GENERATIONS", "1")
    get_app_settings.cache_clear()
    yield
    get_app_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    keys = list(client.scan_iter(match=tenant_key(TENANT_A, "*")))
    if keys:
        client.delete(*keys)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


def _conversation(owner_id: uuid.UUID) -> uuid.UUID:
    with tenant_scope(TENANT_A):
        conversation = make_conversation(tenant_id=TENANT_A, user_id=owner_id, kb_ids=[])
    return uuid.UUID(str(conversation.id))


def _owner() -> uuid.UUID:
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


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": SLUG, "email": OWNER_EMAIL, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


async def _messages(client: httpx.AsyncClient, conversation_id: uuid.UUID, token: str) -> list[Any]:
    response = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


@asynccontextmanager
async def _process_full() -> AsyncIterator[None]:
    """佔滿唯一的名額。上限壓成 1（見 fixture），所以一條就夠——比真的開 64 條快得多，
    而驗到的是同一條判斷。"""
    release = asyncio.Event()
    spawn(release.wait())  # type: ignore[arg-type]
    await asyncio.sleep(0)
    try:
        yield
    finally:
        release.set()


async def _send(
    client: httpx.AsyncClient, conversation_id: uuid.UUID, token: str
) -> httpx.Response:
    return await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"content": "測試問題"},
    )


class TestSaturated:
    async def test_it_refuses_with_429_and_a_retry_hint(self, client: httpx.AsyncClient) -> None:
        owner = await run_orm(_owner)
        conversation_id = await run_orm(_conversation, owner)
        token = await _token(client)

        async with _process_full():
            response = await _send(client, conversation_id, token)

        assert response.status_code == 429, response.text
        assert response.json()["code"] == "RATE_LIMITED"
        assert response.headers["Retry-After"].isdigit(), (
            "429 沒帶 Retry-After——client 只能自己猜，而猜出來的多半是立刻重試"
        )

    async def test_it_leaves_no_half_turn_behind(self, client: httpx.AsyncClient) -> None:
        """**這是這一檔存在的理由。** 擋在建立回合之後的話，使用者會看到一個
        永遠不會有回答的問題，而它還吃掉了一則訊息額度。"""
        owner = await run_orm(_owner)
        conversation_id = await run_orm(_conversation, owner)
        token = await _token(client)
        assert await _messages(client, conversation_id, token) == [], "前提：對話一開始沒有訊息"

        async with _process_full():
            refused = await _send(client, conversation_id, token)

        assert refused.status_code == 429
        assert await _messages(client, conversation_id, token) == [], (
            "被拒的請求留下了訊息——擋線畫在建立回合之後了"
        )

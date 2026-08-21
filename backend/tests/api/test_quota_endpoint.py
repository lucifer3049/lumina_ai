"""驗收：`GET /tenants/current/quota`（09 §2.2，2A-2a）。

配額與用量的即時狀態——使用者在被 429 **之前**該有地方看到「快用完了」。
（80%/100% 的主動通知屬 2A-5；這個端點是它的資料來源。）

形狀是 API 契約：五種資源各一列，`limit`（null＝不限制）、`used`、`remaining`
（null＝不限制）、`resets_at`（null＝無週期）。前端的配額頁與 2A-5 的通知
都讀這一個，各算各的一定對不上。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from api.main import create_app
from common.passwords import hash_password
from core.db import run_orm
from core.redis import get_redis, tenant_key
from services.platform.quota import RESOURCES
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG = "tenant-a"
OWNER_EMAIL = "owner@example.com"


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    keys = list(client.scan_iter(match=tenant_key(TENANT_A, "*")))
    if keys:
        client.delete(*keys)


def _owner(quota: dict[str, Any] | None = None) -> uuid.UUID:
    from apps.identity.models import Role

    ensure_identity_seed()
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG, settings={"quota": quota} if quota is not None else {})
        user = make_user(
            tenant_id=TENANT_A, email=OWNER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=user, role=Role.objects.get(tenant__isnull=True, name="owner"))
    return uuid.UUID(str(user.id))


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


class TestQuotaEndpoint:
    async def test_the_shape_covers_every_resource(self, client: httpx.AsyncClient) -> None:
        await run_orm(_owner)
        token = await _token(client)

        response = await client.get(
            "/api/v1/tenants/current/quota", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200, response.text
        items = {item["resource"]: item for item in response.json()["items"]}
        assert set(items) == set(RESOURCES)
        for item in items.values():
            assert {"resource", "limit", "used", "remaining", "resets_at"} <= set(item)

    async def test_free_plan_limits_and_zero_usage(self, client: httpx.AsyncClient) -> None:
        await run_orm(_owner)
        token = await _token(client)

        response = await client.get(
            "/api/v1/tenants/current/quota", headers={"Authorization": f"Bearer {token}"}
        )

        items = {item["resource"]: item for item in response.json()["items"]}
        assert items["tokens_month"]["limit"] == 1_000_000
        assert items["tokens_month"]["used"] == 0
        assert items["tokens_month"]["remaining"] == 1_000_000
        assert items["tokens_month"]["resets_at"] is not None, "月週期要有下次重置時間"
        assert items["documents"]["resets_at"] is None, "存量資源沒有週期"

    async def test_an_unlimited_resource_reads_as_null(self, client: httpx.AsyncClient) -> None:
        """null＝不限制。用 -1 或省略那一列的話，前端要為「特例」寫分支，而分支
        寫錯的畫面是「已用 3 / 上限 -1」。"""
        await run_orm(_owner, {"documents": None})
        token = await _token(client)

        response = await client.get(
            "/api/v1/tenants/current/quota", headers={"Authorization": f"Bearer {token}"}
        )

        items = {item["resource"]: item for item in response.json()["items"]}
        assert items["documents"]["limit"] is None
        assert items["documents"]["remaining"] is None

    async def test_it_requires_authentication(self, client: httpx.AsyncClient) -> None:
        await run_orm(_owner)

        response = await client.get("/api/v1/tenants/current/quota")

        assert response.status_code == 401

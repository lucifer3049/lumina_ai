"""驗收：`GET /audit-logs`（09 §2.6、04 §8.3，2A-4）。

稽核只有一個對外動作：**查**（04 §8.3 的 Interface 就只有 `query`）。沒有寫入
端點、沒有刪除端點——這不是還沒做，是稽核之所以是稽核的原因。

權限是新的 code（`audit:read`，owner／admin，開工前人類核可）：界線同
`analytics:read`——admin 本來就在管使用者（建立、停用），看稽核是同一份職務。

三件錯了都不會有例外：

1. **分頁沒有穩定排序**。稽核列的時間戳會撞（同一次請求寫的批次、同一秒的
   大量嘗試），只用時間當游標會讓某些列在翻頁時消失或重複——而查稽核的人
   正是在數「他到底試了幾次」。
2. **過濾條件被忽略**。`?resource_id=` 沒接上時回的是「全部」，看起來像
   「這份文件被動過很多次」。
3. **跨租戶**。RLS 是最後一道，但 Repository 的 filter 才是第一道（05 §5.1）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from apps.platform.models import AuditLog
from common.passwords import hash_password
from core.db import run_orm
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG_A = "tenant-a"
SLUG_B = "tenant-b"
PATH = "/api/v1/audit-logs"

_ACTOR = uuid.uuid4()
_OTHER_ACTOR = uuid.uuid4()
_KB_ID = uuid.uuid4()


def _seed_roles_sync() -> None:
    from apps.identity.models import Role

    ensure_identity_seed()
    for tenant_id, slug in ((TENANT_A, SLUG_A), (TENANT_B, SLUG_B)):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=slug)
            for role_name in ("owner", "admin", "editor", "viewer"):
                user = make_user(
                    tenant_id=tenant_id,
                    email=f"{role_name}@example.com",
                    password_hash=hash_password(PASSWORD),
                )
                make_user_role(
                    user=user, role=Role.objects.get(tenant__isnull=True, name=role_name)
                )


def _row_sync(tenant_id: uuid.UUID, *, seconds_ago: int = 0, **overrides: Any) -> AuditLog:
    """直接寫一列（append-only 表不能事後 UPDATE 調時間，所以 created_at
    在 INSERT 當下就給）。"""
    fields: dict[str, Any] = {
        "tenant_id": tenant_id,
        "actor_id": _ACTOR,
        "actor_type": "user",
        "action": "knowledge_base.delete",
        "resource_type": "knowledge_base",
        "resource_id": _KB_ID,
        "before": {"name": "法規"},
        "after": None,
        "outcome": "succeeded",
        "status": 204,
        "permission": None,
        "ip": "203.0.113.7",
        "user_agent": "pytest/1.0",
        "request_id": uuid.uuid4().hex,
        "created_at": datetime.now(UTC) - timedelta(seconds=seconds_ago),
    }
    fields.update(overrides)
    with tenant_scope(tenant_id):
        return AuditLog.objects.create(**fields)


async def _seed_roles() -> None:
    """ORM 一律經 `run_orm`：async 測試裡直接呼叫同步 ORM 會被 Django 擋下
    （SynchronousOnlyOperation），同 tests/api/test_analytics_endpoints.py。"""
    await run_orm(_seed_roles_sync)


async def _row(tenant_id: uuid.UUID, **kwargs: Any) -> AuditLog:
    return await run_orm(_row_sync, tenant_id, **kwargs)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    from api.main import create_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


async def _token(client: httpx.AsyncClient, role: str = "owner", slug: str = SLUG_A) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": slug, "email": f"{role}@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


async def _get(client: httpx.AsyncClient, token: str, **params: Any) -> httpx.Response:
    return await client.get(PATH, headers={"Authorization": f"Bearer {token}"}, params=params)


class TestPermissions:
    @pytest.mark.parametrize(
        ("role", "expected"),
        [("owner", 200), ("admin", 200), ("editor", 403), ("viewer", 403)],
    )
    async def test_only_owner_and_admin_may_read(
        self, client: httpx.AsyncClient, role: str, expected: int
    ) -> None:
        await _seed_roles()
        token = await _token(client, role)

        response = await _get(client, token)

        assert response.status_code == expected, response.text

    async def test_it_requires_authentication(self, client: httpx.AsyncClient) -> None:
        await _seed_roles()

        assert (await client.get(PATH)).status_code == 401


class TestListing:
    async def test_items_are_newest_first(self, client: httpx.AsyncClient) -> None:
        await _seed_roles()
        await _row(TENANT_A, seconds_ago=30, action="user.create")
        await _row(TENANT_A, seconds_ago=10, action="user.update")
        await _row(TENANT_A, seconds_ago=0, action="user.deactivate")
        token = await _token(client)

        items = (await _get(client, token)).json()["items"]

        # 登入自己也是一列（auth.login），所以只比對相對順序。
        actions = [item["action"] for item in items]
        assert actions.index("user.deactivate") < actions.index("user.update")
        assert actions.index("user.update") < actions.index("user.create")

    async def test_the_row_shape_carries_the_investigation_fields(
        self, client: httpx.AsyncClient
    ) -> None:
        """稽核列要能單獨回答「誰、何時、從哪、對什麼、結果如何」——
        少一個欄位就得回頭翻 log，而稽核的使用者不一定拿得到 log。"""
        await _seed_roles()
        await _row(TENANT_A)
        token = await _token(client)

        items = (await _get(client, token, action="knowledge_base.delete")).json()["items"]

        assert len(items) == 1
        item = items[0]
        assert item["actor_id"] == str(_ACTOR)
        assert item["actor_type"] == "user"
        assert item["resource_type"] == "knowledge_base"
        assert item["resource_id"] == str(_KB_ID)
        assert item["outcome"] == "succeeded"
        assert item["status"] == 204
        assert item["before"] == {"name": "法規"}
        assert item["after"] is None
        assert item["ip"] == "203.0.113.7"
        assert item["request_id"]
        assert item["created_at"]

    async def test_pagination_walks_every_row_exactly_once(self, client: httpx.AsyncClient) -> None:
        await _seed_roles()
        for index in range(5):
            await _row(TENANT_A, seconds_ago=index, request_id=f"req-{index}")
        token = await _token(client)

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):  # 上限只是防呆，正常兩三頁就走完
            params: dict[str, Any] = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            page = (await _get(client, token, **params)).json()
            seen.extend(item["id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break

        assert cursor is None, "游標沒有終點——最後一頁必須回 next_cursor=null"
        assert len(seen) == len(set(seen)), "翻頁出現重複列"
        # 5 列稽核 + 1 列 auth.login
        assert len(seen) == 6

    async def test_limit_is_capped(self, client: httpx.AsyncClient) -> None:
        """`limit` 直接進 SQL，沒有上限時一個極大值不會失敗，只會讓一次查詢
        把整個月的分區拉進記憶體（同 09 §1.1 的上限 100）。"""
        await _seed_roles()
        token = await _token(client)

        assert (await _get(client, token, limit=101)).status_code == 422
        assert (await _get(client, token, limit=0)).status_code == 422


class TestFilters:
    async def test_filter_by_action(self, client: httpx.AsyncClient) -> None:
        await _seed_roles()
        await _row(TENANT_A, action="user.create")
        await _row(TENANT_A, action="knowledge_base.delete")
        token = await _token(client)

        items = (await _get(client, token, action="user.create")).json()["items"]

        assert [item["action"] for item in items] == ["user.create"]

    async def test_filter_by_resource(self, client: httpx.AsyncClient) -> None:
        """「這份知識庫被誰動過」——05 §4 的第二組索引就是為了這個查法。"""
        await _seed_roles()
        await _row(TENANT_A)
        await _row(TENANT_A, resource_id=uuid.uuid4())
        token = await _token(client)

        items = (
            await _get(client, token, resource_type="knowledge_base", resource_id=str(_KB_ID))
        ).json()["items"]

        assert [item["resource_id"] for item in items] == [str(_KB_ID)]

    async def test_filter_by_actor(self, client: httpx.AsyncClient) -> None:
        await _seed_roles()
        await _row(TENANT_A)
        await _row(TENANT_A, actor_id=_OTHER_ACTOR)
        token = await _token(client)

        items = (await _get(client, token, actor_id=str(_ACTOR))).json()["items"]

        assert [item["actor_id"] for item in items] == [str(_ACTOR)]

    async def test_filter_by_date_range(self, client: httpx.AsyncClient) -> None:
        await _seed_roles()
        await _row(TENANT_A)
        token = await _token(client)
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()

        params = {"from": tomorrow}
        response = await client.get(
            PATH, headers={"Authorization": f"Bearer {token}"}, params=params
        )

        assert response.status_code == 200, response.text
        assert response.json()["items"] == []


class TestTenantIsolation:
    async def test_a_tenant_never_sees_another_tenants_audit_trail(
        self, client: httpx.AsyncClient
    ) -> None:
        await _seed_roles()
        await _row(TENANT_A, request_id="mine")
        await _row(TENANT_B, request_id="theirs")
        token = await _token(client)

        items = (await _get(client, token, action="knowledge_base.delete")).json()["items"]

        assert [item["request_id"] for item in items] == ["mine"]

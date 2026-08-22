"""驗收：`GET /notifications`、`PATCH /notifications/{id}/read`（09 §2.6，2A-5）。

09 §2.6 把這兩個端點的權限寫成「**登入者**」——不是新的 permission code，因為
收件匣裡的東西本來就只寄給一個人。這也是它與 `/audit-logs`、`/analytics/*` 的分野：
那兩個是管理面（owner／admin），這一個是每個人自己的。

因此「權限」在這裡的形狀是**擁有者判定**，不是角色判定，而它有一個容易寫錯的地方：
別人的通知 id 要回 **404 而不是 403**。403 等於承認「這個 id 存在、只是不給你」，
而通知 id 是可以猜的——連續請求就能數出別人收到幾則。

三件錯了都不會有例外：

1. **收件匣沒有依 user 過濾**。同租戶的每個人看到彼此的通知，而畫面完全正常
   ——只是「怎麼有一則不是我的文件」。
2. **未讀數與清單各查各的**。兩次查詢之間有新通知進來時，鈴鐺上的數字與點開
   之後看到的對不起來，而使用者只會覺得這個系統怪怪的。
3. **標已讀被記進稽核**。它是本人對自己收件匣的動作、高頻，記下來只會把真正的
   敏感操作淹掉（豁免的理由與 `conversations_*` 同一類，見 2A-4 的 `AUDIT_EXEMPT`）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from services.platform.notifications import TYPE_DOCUMENT_READY

from apps.platform.models import AuditLog, Notification
from common.passwords import hash_password
from core.db import run_orm
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG_A = "tenant-a"
SLUG_B = "tenant-b"
PATH = "/api/v1/notifications"


def _seed_sync() -> None:
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


def _user_id_sync(tenant_id: uuid.UUID, role: str) -> uuid.UUID:
    from apps.identity.models import User

    with tenant_scope(tenant_id):
        return uuid.UUID(str(User.objects.get(email=f"{role}@example.com").id))


def _row_sync(tenant_id: uuid.UUID, user_id: uuid.UUID, **overrides: Any) -> Notification:
    fields: dict[str, Any] = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "type": TYPE_DOCUMENT_READY,
        "title": "法規手冊.pdf 已完成",
        "body": "可以開始問答了。",
        "channels": ["in_app"],
        "meta": {"count": 1},
        "dedupe_key": None,
    }
    fields.update(overrides)
    with tenant_scope(tenant_id):
        return Notification.objects.create(**fields)


async def _seed() -> None:
    """ORM 一律經 `run_orm`（async 測試直接呼叫同步 ORM 會被 Django 擋下）。"""
    await run_orm(_seed_sync)


async def _user_id(role: str, tenant_id: uuid.UUID = TENANT_A) -> uuid.UUID:
    return await run_orm(_user_id_sync, tenant_id, role)


async def _row(tenant_id: uuid.UUID, user_id: uuid.UUID, **overrides: Any) -> Notification:
    return await run_orm(_row_sync, tenant_id, user_id, **overrides)


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


async def _read(
    client: httpx.AsyncClient, token: str, notification_id: uuid.UUID
) -> httpx.Response:
    return await client.patch(
        f"{PATH}/{notification_id}/read", headers={"Authorization": f"Bearer {token}"}
    )


class TestAccess:
    async def test_it_requires_authentication(self, client: httpx.AsyncClient) -> None:
        response = await client.get(PATH)

        assert response.status_code == 401

    @pytest.mark.parametrize("role", ["owner", "admin", "editor", "viewer"])
    async def test_every_signed_in_role_has_an_inbox(
        self, client: httpx.AsyncClient, role: str
    ) -> None:
        """09 §2.6 的權限欄就是「登入者」——viewer 也會收到自己上傳的文件的通知。"""
        await _seed()
        token = await _token(client, role)

        response = await _get(client, token)

        assert response.status_code == 200, response.text

    async def test_it_only_returns_my_own_notifications(self, client: httpx.AsyncClient) -> None:
        await _seed()
        await _row(TENANT_A, await _user_id("owner"), title="owner 的")
        await _row(TENANT_A, await _user_id("admin"), title="admin 的")
        token = await _token(client, "owner")

        response = await _get(client, token)

        assert [item["title"] for item in response.json()["items"]] == ["owner 的"]

    async def test_another_tenant_is_invisible(self, client: httpx.AsyncClient) -> None:
        """同名角色在 B 租戶也有一個 owner——Repository 的 filter 是第一道，
        RLS 是最後一道（05 §5.1）。"""
        await _seed()
        await _row(TENANT_B, await _user_id("owner", TENANT_B), title="B 的")
        token = await _token(client, "owner")

        response = await _get(client, token)

        assert response.json()["items"] == []


class TestInbox:
    async def test_the_unread_count_comes_with_the_page(self, client: httpx.AsyncClient) -> None:
        """鈴鐺上的數字與清單必須出自同一次查詢：分成兩個請求時，兩者之間
        進來的新通知會讓數字與內容對不起來。"""
        await _seed()
        user_id = await _user_id("owner")
        for index in range(3):
            await _row(TENANT_A, user_id, title=f"第 {index} 則")
        token = await _token(client, "owner")

        body = (await _get(client, token, limit=2)).json()

        assert body["unread_count"] == 3
        assert len(body["items"]) == 2

    async def test_paging_walks_every_row_exactly_once(self, client: httpx.AsyncClient) -> None:
        await _seed()
        user_id = await _user_id("owner")
        for index in range(5):
            await _row(TENANT_A, user_id, title=f"第 {index} 則")
        token = await _token(client, "owner")

        first = (await _get(client, token, limit=3)).json()
        second = (await _get(client, token, limit=3, cursor=first["next_cursor"])).json()

        titles = [item["title"] for item in first["items"] + second["items"]]
        assert sorted(titles) == [f"第 {index} 則" for index in range(5)]
        assert second["next_cursor"] is None

    async def test_a_broken_cursor_is_a_422_not_a_500(self, client: httpx.AsyncClient) -> None:
        """游標來自網址與 localStorage，被截斷、被手改都很正常（同 `/audit-logs`）。"""
        await _seed()
        token = await _token(client, "owner")

        response = await _get(client, token, cursor="這不是游標")

        assert response.status_code == 422, response.text

    async def test_unread_only_filters_out_what_has_been_read(
        self, client: httpx.AsyncClient
    ) -> None:
        await _seed()
        user_id = await _user_id("owner")
        read = await _row(TENANT_A, user_id, title="讀過的")
        await _row(TENANT_A, user_id, title="沒讀的")
        token = await _token(client, "owner")
        await _read(client, token, uuid.UUID(str(read.id)))

        body = (await _get(client, token, unread_only=True)).json()

        assert [item["title"] for item in body["items"]] == ["沒讀的"]


class TestMarkRead:
    async def test_it_marks_the_notification_read(self, client: httpx.AsyncClient) -> None:
        await _seed()
        row = await _row(TENANT_A, await _user_id("owner"))
        token = await _token(client, "owner")

        response = await _read(client, token, uuid.UUID(str(row.id)))

        assert response.status_code == 200, response.text
        assert response.json()["read_at"] is not None

    async def test_reading_twice_keeps_the_first_timestamp(self, client: httpx.AsyncClient) -> None:
        """重複點擊、多開分頁——第二次不該把「什麼時候讀的」改掉，也不該 409。"""
        await _seed()
        row = await _row(TENANT_A, await _user_id("owner"))
        token = await _token(client, "owner")

        first = await _read(client, token, uuid.UUID(str(row.id)))
        second = await _read(client, token, uuid.UUID(str(row.id)))

        assert second.status_code == 200
        assert second.json()["read_at"] == first.json()["read_at"]

    async def test_someone_elses_notification_is_a_404(self, client: httpx.AsyncClient) -> None:
        """**不是 403**：403 等於承認這個 id 存在，而通知 id 猜得到——
        連續請求就能數出別人收到幾則、什麼時候收到。"""
        await _seed()
        row = await _row(TENANT_A, await _user_id("admin"))
        token = await _token(client, "owner")

        response = await _read(client, token, uuid.UUID(str(row.id)))

        assert response.status_code == 404, response.text

    async def test_another_tenants_notification_is_a_404(self, client: httpx.AsyncClient) -> None:
        await _seed()
        row = await _row(TENANT_B, await _user_id("owner", TENANT_B))
        token = await _token(client, "owner")

        response = await _read(client, token, uuid.UUID(str(row.id)))

        assert response.status_code == 404, response.text

    async def test_it_does_not_flood_the_audit_trail(self, client: httpx.AsyncClient) -> None:
        """寫入型請求預設全記（2A-4），標已讀是明示豁免的一條：它是本人對自己
        收件匣的狀態變更、高頻，記下來只會把真正的敏感操作淹掉。"""
        await _seed()
        row = await _row(TENANT_A, await _user_id("owner"))
        token = await _token(client, "owner")

        await _read(client, token, uuid.UUID(str(row.id)))

        count = await run_orm(_audit_count_sync)
        assert count == 0


def _audit_count_sync() -> int:
    with tenant_scope(TENANT_A):
        return int(AuditLog.objects.filter(resource_type="notification").count())

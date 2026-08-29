"""驗收：`/settings` 的 provider 憑證（09 §2.6「唯寫不回讀明文」，工作包 2C-2）。

2C-1 把 `/settings` 做成了「參數 + 配額」，本包在同一支端點上加**第三種東西**，而它
與前兩種有一個根本差別：**寫得進去、讀不回來**。

三條防線，每一條都對應一種「東西已經外流了、而一切看起來正常」的情況：

1. **回應不得帶明文**。GET 回的是遮罩（是否設定過、末四碼、更新時間）。回明文的話，
   任何一個拿得到 `tenant:admin` 的人都可以把整個租戶的 provider 金鑰抄走，而稽核上
   只是一次正常的讀取。
2. **稽核不得帶明文**。2C-1 的 `settings.update` 記 before/after 整份設定——憑證若走
   同一條路，金鑰會逐字落進 `platform_auditlog`，而那張表是**刻意不可刪改**的
   （05 §3.5）。這是本包最容易犯、也最難收拾的一個錯。
3. **憑證不進 `tenant.settings`**。那一欄會整份回給前端（2C-1 的 GET），也會被
   `param_config` 讀去解析參數。存進去就是前兩條同時失守。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from api.main import create_app
from common.passwords import hash_password
from core.db import run_orm
from core.redis import get_redis, tenant_key
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG_A = "tenant-a"
OWNER_EMAIL = "owner@example.com"
EDITOR_EMAIL = "editor@example.com"
SECRET = "sk-live-0123456789abcdef"


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    keys = list(client.scan_iter(match=tenant_key(TENANT_A, "*")))
    if keys:
        client.delete(*keys)


@pytest.fixture
def tenant_a_with_roles() -> None:
    ensure_identity_seed()
    from apps.identity.models import Role

    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG_A)
        for email, role_name in ((OWNER_EMAIL, "owner"), (EDITOR_EMAIL, "editor")):
            user = make_user(tenant_id=TENANT_A, email=email, password_hash=hash_password(PASSWORD))
            make_user_role(user=user, role=Role.objects.get(tenant__isnull=True, name=role_name))


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
    return {"Authorization": f"Bearer {token}"}


def _stored_settings() -> dict[str, Any]:
    from apps.identity.models import Tenant

    with tenant_scope(TENANT_A):
        return dict(Tenant.objects.get(id=TENANT_A).settings or {})


def _audit_rows() -> list[str]:
    from apps.platform.models import AuditLog

    with tenant_scope(TENANT_A):
        return [repr(row.before) + repr(row.after) for row in AuditLog.objects.all()]


async def _write_secret(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.patch(
        "/api/v1/settings",
        json={"settings": {"credentials": {"openai_api_key": SECRET}}},
        headers=_auth(token),
    )


class TestWriteOnly:
    async def test_a_credential_can_be_written(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        response = await _write_secret(client, await _token(client))

        assert response.status_code == 200, response.text

    async def test_the_response_never_echoes_the_secret(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """**寫入的回應也算回讀**：多數 client 會把 PATCH 的回應直接顯示在畫面上。"""
        response = await _write_secret(client, await _token(client))

        assert SECRET not in response.text

    async def test_get_returns_a_mask_not_the_secret(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        token = await _token(client)
        await _write_secret(client, token)

        response = await client.get("/api/v1/settings", headers=_auth(token))

        assert response.status_code == 200
        assert SECRET not in response.text
        described = response.json()["credentials"]
        # 畫面要分得出「沒設過」與「設過了」，而末四碼讓人認得出是哪一把。
        assert described[0]["name"] == "openai_api_key"
        assert described[0]["hint"] == SECRET[-4:]

    async def test_an_unset_credential_is_simply_absent(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        response = await client.get("/api/v1/settings", headers=_auth(await _token(client)))

        assert response.json()["credentials"] == []


class TestItDoesNotLeak:
    async def test_the_secret_does_not_land_in_tenant_settings(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """**本檔第 3 條。** 那一欄會整份回給前端，也會被 `param_config` 讀去。"""
        await _write_secret(client, await _token(client))

        assert SECRET not in repr(await run_orm(_stored_settings))

    async def test_the_secret_does_not_land_in_the_audit_log(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """**本檔第 2 條，也是本包最容易犯的錯。** 2C-1 的稽核記的是整份 before/after，
        而 `platform_auditlog` 是刻意不可刪改的——寫進去就收不回來。"""
        await _write_secret(client, await _token(client))

        rows = await run_orm(_audit_rows)
        assert rows, "設定變更本來就該留稽核（2C-1）"
        assert all(SECRET not in row for row in rows)

    async def test_the_audit_still_records_that_it_changed(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """遮罩不等於不記：「誰在什麼時候換掉了 provider 金鑰」正是稽核最該有的一列。"""
        await _write_secret(client, await _token(client))

        rows = await run_orm(_audit_rows)
        assert any("openai_api_key" in row for row in rows), "連換了哪一把都看不到"


class TestValidation:
    async def test_an_unknown_credential_name_is_rejected(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """名字是白名單：打錯的話它會存進去、在畫面上看得見，而沒有任何東西會去讀它
        ——與 2B-5 擋 `retreival` 是同一種錯誤。"""
        response = await client.patch(
            "/api/v1/settings",
            json={"settings": {"credentials": {"openai_key": SECRET}}},
            headers=_auth(await _token(client)),
        )

        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["field"] == "settings.credentials.openai_key"

    async def test_an_empty_secret_is_rejected(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """空字串存得進去、`configured` 會顯示 true，而每一次呼叫 provider 都是 401。"""
        response = await client.patch(
            "/api/v1/settings",
            json={"settings": {"credentials": {"openai_api_key": "   "}}},
            headers=_auth(await _token(client)),
        )

        assert response.status_code == 422

    async def test_null_clears_a_credential(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """撤銷要有出路，而且是明確的 `null`——與「這次沒送」分得開。"""
        token = await _token(client)
        await _write_secret(client, token)

        cleared = await client.patch(
            "/api/v1/settings",
            json={"settings": {"credentials": {"openai_api_key": None}}},
            headers=_auth(token),
        )

        assert cleared.status_code == 200, cleared.text
        response = await client.get("/api/v1/settings", headers=_auth(token))
        assert response.json()["credentials"] == []


class TestPermissions:
    async def test_editor_cannot_write_credentials(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        response = await _write_secret(client, await _token(client, EDITOR_EMAIL))

        assert response.status_code == 403

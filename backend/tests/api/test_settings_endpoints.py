"""驗收：`GET/PATCH /settings` 端點（09 §2.6，工作包 2C-1）。

09 §2.6 那一列自始就寫著「租戶級設定 | tenant:admin」，而它至今不存在——三層覆寫的
中間層因此讀得到（`tenant.settings` 從 2A 起就有 `quota`）卻沒有人填得進去。

這一層釘的是**契約**，不是覆寫邏輯（那在 unit 與 integration 兩檔）：

1. **`tenant:admin` 而不是 `tenant:read`**。改租戶層參數會改變**整個租戶**每一個人
   問到的答案——破壞範圍比改單一 KB 更大，而 KB 的 PATCH 已經是 `knowledge:admin`。
2. **422 的形狀**：`errors[]` 逐欄位、`field` 為 `settings.<區>.<鍵>`，與 FastAPI 的
   `loc` 同形。2C-4 的畫面上同時有租戶層與 KB 層兩組輸入，只回一句「設定不合法」的
   話，它標不到是哪一格。
3. **稽核**。租戶層的參數變更影響所有人，而症狀（「最近答得怪怪的」）與這次變更之間
   隔著幾天——那時唯一查得到「誰、什麼時候、從什麼改成什麼」的地方就是稽核（2A-4）。
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
VIEWER_EMAIL = "viewer@example.com"


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
        for email, role_name in (
            (OWNER_EMAIL, "owner"),
            (EDITOR_EMAIL, "editor"),
            (VIEWER_EMAIL, "viewer"),
        ):
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


def _audit_entry() -> Any:
    from apps.platform.models import AuditLog

    with tenant_scope(TENANT_A):
        return AuditLog.objects.filter(action="settings.update").first()


class TestRead:
    async def test_a_fresh_tenant_reads_back_an_empty_object(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        response = await client.get("/api/v1/settings", headers=_auth(await _token(client)))

        assert response.status_code == 200, response.text
        assert response.json()["settings"] == {}

    async def test_it_reads_back_what_was_written(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        token = await _token(client)
        await client.patch(
            "/api/v1/settings",
            json={"settings": {"retrieval": {"top_k": 12}}},
            headers=_auth(token),
        )

        response = await client.get("/api/v1/settings", headers=_auth(token))

        assert response.json()["settings"] == {"retrieval": {"top_k": 12}}


class TestWrite:
    async def test_a_valid_patch_returns_the_new_state(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        response = await client.patch(
            "/api/v1/settings",
            json={"settings": {"retrieval": {"top_k": 12}, "chunk": {"target_tokens": 256}}},
            headers=_auth(await _token(client)),
        )

        assert response.status_code == 200, response.text
        assert response.json()["settings"]["chunk"] == {"target_tokens": 256}

    async def test_a_bad_value_is_422_with_per_field_errors(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        response = await client.patch(
            "/api/v1/settings",
            json={"settings": {"retrieval": {"top_k": 99999}}},
            headers=_auth(await _token(client)),
        )

        assert response.status_code == 422, response.text
        body = response.json()
        assert body["code"] == "VALIDATION_FAILED"
        assert body["errors"][0]["field"] == "settings.retrieval.top_k"
        # 訊息要帶得出允許的範圍——只說「超出範圍」的話，使用者得靠猜的。
        assert "200" in body["errors"][0]["message"]

    async def test_an_unknown_section_is_rejected(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        response = await client.patch(
            "/api/v1/settings",
            json={"settings": {"retreival": {"top_k": 12}}},
            headers=_auth(await _token(client)),
        )

        assert response.status_code == 422
        assert response.json()["errors"][0]["field"] == "settings.retreival"


class TestPermissions:
    async def test_editor_cannot_read_the_tenant_settings(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """09 §2.6 的那一列是 `tenant:admin`——這裡日後會住 provider 憑證（2C-2）。"""
        response = await client.get(
            "/api/v1/settings", headers=_auth(await _token(client, EDITOR_EMAIL))
        )

        assert response.status_code == 403

    async def test_viewer_cannot_write(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        response = await client.patch(
            "/api/v1/settings",
            json={"settings": {"retrieval": {"top_k": 12}}},
            headers=_auth(await _token(client, VIEWER_EMAIL)),
        )

        assert response.status_code == 403

    async def test_it_requires_authentication(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/v1/settings")).status_code == 401


class TestAudit:
    async def test_a_settings_change_is_audited_with_before_and_after(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        await client.patch(
            "/api/v1/settings",
            json={"settings": {"retrieval": {"top_k": 12}}},
            headers=_auth(await _token(client)),
        )

        entry = await run_orm(_audit_entry)
        assert entry is not None, "租戶層設定變更沒有留下稽核紀錄"
        # before/after 兩邊都要在：只記 after 的話，事後看得到「現在是 12」，
        # 看不到「本來是多少」——而那正是查「什麼時候變差的」要的東西。
        assert entry.before is not None
        assert entry.after["retrieval"] == {"top_k": 12}


class TestContractStability:
    def test_the_operation_ids_are_declared(self) -> None:
        schema = create_app().openapi()
        operations = {
            operation["operationId"]
            for path in schema["paths"].values()
            for operation in path.values()
            if isinstance(operation, dict) and "operationId" in operation
        }

        assert {"settings_get", "settings_update"} <= operations

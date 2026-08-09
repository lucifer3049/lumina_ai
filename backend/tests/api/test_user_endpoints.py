"""驗收：`/users` 與 `/tenants/current` 的行為（09 §2.2）。

與 `test_permission_enforcement.py` 的分工：那一檔驗「誰進得來」，本檔驗
「進來之後做的事對不對」。分開的理由是它們的失敗原因完全不同——前者是權限
宣告漏掉，後者是業務邏輯寫錯，混在一起的表會變得沒有人維護。

本檔的重點放在四件**做錯了也不會報錯**的事：

1. 建立使用者時把密碼原樣存進去（而不是雜湊）。
2. 停用帳號之後對方手上的 token 還能用——「停用」變成只是個狀態欄位。
3. 改密碼之後舊 token 還能用（同上，但更常見：只更新欄位、忘了撤銷）。
4. 回應把 ``password_hash`` 一起吐出來——序列化整個 model 時的典型意外。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from api.main import create_app
from common.passwords import hash_password, verify_password
from core.redis import get_redis, tenant_key
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "an entirely different passphrase"
SLUG_A = "tenant-a"
OWNER_EMAIL = "owner@example.com"
MEMBER_EMAIL = "member@example.com"


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    keys = list(client.scan_iter(match=tenant_key(TENANT_A, "*")))
    if keys:
        client.delete(*keys)


@pytest.fixture
def tenant_with_owner_and_member() -> dict[str, uuid.UUID]:
    ensure_identity_seed()
    from apps.identity.models import Role

    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG_A)
        owner = make_user(
            tenant_id=TENANT_A, email=OWNER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=owner, role=Role.objects.get(tenant__isnull=True, name="owner"))
        member = make_user(
            tenant_id=TENANT_A, email=MEMBER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=member, role=Role.objects.get(tenant__isnull=True, name="viewer"))
    return {"owner": owner.id, "member": member.id}


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_app(enable_spike_endpoints=False), raise_app_exceptions=False
        ),
        base_url="http://testserver",
    ) as c:
        yield c


async def _login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": SLUG_A, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── 建立與讀取 ──────────────────────────────────────────────────


class TestCreateUser:
    async def test_password_is_stored_hashed(
        self, client: httpx.AsyncClient, tenant_with_owner_and_member: dict[str, uuid.UUID]
    ) -> None:
        """DB 裡存的必須是雜湊，而且驗得過原密碼。

        直接存明文不會有任何錯誤——登入照樣成功（因為比對的也是明文），
        只有資料庫外洩時才會發現。所以這條測試繞過 API 直接看資料列。
        """
        from apps.identity.models import User
        from core.uow import unit_of_work

        token = await _login(client, OWNER_EMAIL)

        response = await client.post(
            "/api/v1/users",
            json={"email": "fresh@example.com", "display_name": "Fresh", "password": PASSWORD},
            headers=_auth(token),
        )

        assert response.status_code == 201

        def read_hash() -> str:
            with tenant_scope(TENANT_A), unit_of_work():
                return str(User.objects.get(email="fresh@example.com").password_hash)

        from asgiref.sync import sync_to_async

        stored = await sync_to_async(read_hash, thread_sensitive=False)()

        assert stored != PASSWORD, "密碼以明文存入"
        assert verify_password(PASSWORD, stored)

    async def test_response_never_exposes_the_password_hash(
        self, client: httpx.AsyncClient, tenant_with_owner_and_member: dict[str, uuid.UUID]
    ) -> None:
        """回應不得出現雜湊（或任何密碼欄位）。

        典型的意外是「把整個 model 序列化」——雜湊本身雖然不能直接登入，但它讓
        離線破解成為可能，而且完全沒有理由送到 client。
        """
        token = await _login(client, OWNER_EMAIL)

        response = await client.post(
            "/api/v1/users",
            json={"email": "fresh@example.com", "display_name": "Fresh", "password": PASSWORD},
            headers=_auth(token),
        )

        assert "password" not in response.text.lower()

    async def test_duplicate_email_in_the_same_tenant_is_rejected(
        self, client: httpx.AsyncClient, tenant_with_owner_and_member: dict[str, uuid.UUID]
    ) -> None:
        """同租戶內 email 唯一（1A-2 的 ``UNIQUE(tenant_id, email)``）。

        回 409 而不是 500：這是使用者可以自己修正的情況（換一個信箱），
        讓 DB 的唯一約束直接冒成 500 等於把可預期的衝突當成系統故障。
        """
        token = await _login(client, OWNER_EMAIL)

        response = await client.post(
            "/api/v1/users",
            json={"email": MEMBER_EMAIL, "display_name": "Dup", "password": PASSWORD},
            headers=_auth(token),
        )

        assert response.status_code == 409
        assert response.json()["code"] == "RESOURCE_CONFLICT"

    async def test_created_user_can_log_in(
        self, client: httpx.AsyncClient, tenant_with_owner_and_member: dict[str, uuid.UUID]
    ) -> None:
        """建立流程與登入流程要對得起來——端到端的最小驗證。"""
        token = await _login(client, OWNER_EMAIL)
        await client.post(
            "/api/v1/users",
            json={"email": "fresh@example.com", "display_name": "Fresh", "password": PASSWORD},
            headers=_auth(token),
        )

        assert await _login(client, "fresh@example.com")


class TestMe:
    async def test_me_returns_the_caller(
        self, client: httpx.AsyncClient, tenant_with_owner_and_member: dict[str, uuid.UUID]
    ) -> None:
        token = await _login(client, MEMBER_EMAIL)

        response = await client.get("/api/v1/users/me", headers=_auth(token))

        assert response.json()["email"] == MEMBER_EMAIL
        assert response.json()["roles"] == ["viewer"]

    async def test_me_can_update_own_profile_without_any_permission_code(
        self, client: httpx.AsyncClient, tenant_with_owner_and_member: dict[str, uuid.UUID]
    ) -> None:
        """viewer 沒有 ``user:write``，但改自己的名字不需要那個權限。

        把個人資料綁在 ``user:write`` 之下是常見的誤設，後果是「一般員工連自己的
        顯示名稱都不能改」，而那個權限一旦補給他，他就同時能建立與停用別人的帳號。
        """
        token = await _login(client, MEMBER_EMAIL)

        response = await client.patch(
            "/api/v1/users/me", json={"display_name": "改過的名字"}, headers=_auth(token)
        )

        assert response.status_code == 200
        assert response.json()["display_name"] == "改過的名字"


# ── 停用與密碼變更：兩者都必須讓既有 token 立刻失效 ──────────────


class TestDeactivate:
    async def test_deactivated_user_cannot_log_in(
        self, client: httpx.AsyncClient, tenant_with_owner_and_member: dict[str, uuid.UUID]
    ) -> None:
        owner_token = await _login(client, OWNER_EMAIL)

        response = await client.post(
            f"/api/v1/users/{tenant_with_owner_and_member['member']}/deactivate",
            headers=_auth(owner_token),
        )
        assert response.status_code == 204

        login = await client.post(
            "/api/v1/auth/login",
            json={"tenant_slug": SLUG_A, "email": MEMBER_EMAIL, "password": PASSWORD},
        )

        assert login.status_code == 401

    async def test_deactivation_kills_the_existing_access_token_immediately(
        self, client: httpx.AsyncClient, tenant_with_owner_and_member: dict[str, uuid.UUID]
    ) -> None:
        """**立刻**，不是等 15 分鐘。

        這是 1A-3 留下的缺口：當時 ``token_version`` 只在換發時檢查，所以停用之後
        對方手上那張 access token 還能用到過期為止。對「員工離職、當場停用帳號」
        這個情境來說，15 分鐘的空窗是不能接受的——而它完全沒有症狀，除非你正好
        在那 15 分鐘內測試。
        """
        member_token = await _login(client, MEMBER_EMAIL)
        owner_token = await _login(client, OWNER_EMAIL)

        assert (
            await client.get("/api/v1/users/me", headers=_auth(member_token))
        ).status_code == 200

        await client.post(
            f"/api/v1/users/{tenant_with_owner_and_member['member']}/deactivate",
            headers=_auth(owner_token),
        )

        response = await client.get("/api/v1/users/me", headers=_auth(member_token))

        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_TOKEN_REVOKED"

    async def test_deactivating_does_not_touch_other_users_sessions(
        self, client: httpx.AsyncClient, tenant_with_owner_and_member: dict[str, uuid.UUID]
    ) -> None:
        """撤銷的範圍是「那個帳號」，不是整個租戶。

        這條看起來理所當然，但實作上很容易把 Redis 的撤銷標記寫成租戶層級的 key
        （少了 user id 那一段），於是停用一個人會把全公司都登出。
        """
        member_token = await _login(client, MEMBER_EMAIL)
        owner_token = await _login(client, OWNER_EMAIL)

        await client.post(
            f"/api/v1/users/{tenant_with_owner_and_member['member']}/deactivate",
            headers=_auth(owner_token),
        )

        assert (await client.get("/api/v1/users/me", headers=_auth(owner_token))).status_code == 200
        assert member_token  # 上一條已驗它失效，這裡只確認 owner 不受牽連


class TestPasswordChange:
    async def test_password_change_requires_the_current_password(
        self, client: httpx.AsyncClient, tenant_with_owner_and_member: dict[str, uuid.UUID]
    ) -> None:
        """要改密碼必須先證明你知道舊的。

        少了這一步，任何一張被偷走的 access token 都能直接把帳號接管過去
        （改掉密碼 = 原主人再也登不進來）。
        """
        token = await _login(client, MEMBER_EMAIL)

        response = await client.post(
            "/api/v1/auth/password/change",
            json={"current_password": "wrong", "new_password": NEW_PASSWORD},
            headers=_auth(token),
        )

        assert response.status_code == 401

    async def test_password_change_revokes_every_existing_session(
        self, client: httpx.AsyncClient, tenant_with_owner_and_member: dict[str, uuid.UUID]
    ) -> None:
        """改密碼的**目的**通常就是「我懷疑帳號被盜」——舊 session 必須全滅。

        只更新欄位而不撤銷的話，攻擊者手上的 token 照樣有效，而使用者以為自己
        已經處理完了。這是本檔最重要的一條。
        """
        first_session = await _login(client, MEMBER_EMAIL)
        second_session = await _login(client, MEMBER_EMAIL)  # 想像成另一台裝置

        changed = await client.post(
            "/api/v1/auth/password/change",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=_auth(first_session),
        )
        assert changed.status_code == 204

        for token in (first_session, second_session):
            response = await client.get("/api/v1/users/me", headers=_auth(token))
            assert response.status_code == 401

    async def test_new_password_works_and_old_one_does_not(
        self, client: httpx.AsyncClient, tenant_with_owner_and_member: dict[str, uuid.UUID]
    ) -> None:
        token = await _login(client, MEMBER_EMAIL)
        await client.post(
            "/api/v1/auth/password/change",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=_auth(token),
        )

        old = await client.post(
            "/api/v1/auth/login",
            json={"tenant_slug": SLUG_A, "email": MEMBER_EMAIL, "password": PASSWORD},
        )

        assert old.status_code == 401
        assert await _login(client, MEMBER_EMAIL, NEW_PASSWORD)


# ── 租戶資訊 ────────────────────────────────────────────────────


class TestTenantCurrent:
    async def test_returns_only_the_callers_tenant(
        self, client: httpx.AsyncClient, tenant_with_owner_and_member: dict[str, uuid.UUID]
    ) -> None:
        """``/tenants/current`` 的「current」來自 token，不是任何參數。

        設計成 ``/tenants/{id}`` 的話，就會多出一個「傳別人的 id 會怎樣」的攻擊面，
        而那條路必須靠每個端點自己記得檢查。沒有 id 就沒有那個問題。
        """
        token = await _login(client, MEMBER_EMAIL)

        response = await client.get("/api/v1/tenants/current", headers=_auth(token))

        assert response.json()["slug"] == SLUG_A
        assert "id" in response.json()

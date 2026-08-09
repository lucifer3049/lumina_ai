"""驗收：權限矩陣在端點上真的生效（10 §3、09 §2.2）。

**矩陣式測試的理由**：權限是「角色 × 端點」的二維問題，逐條寫測試必然會漏，
而漏掉的那一格不會有任何症狀——沒有人測過的組合，通常就是被放行的那一個。
列成表之後，新增端點忘了宣告權限會讓表少一列，加角色會讓表少一欄，兩者都看得見。

**403 而不是 404**（10 §3）：這裡全部是「功能類」權限——你不能建立使用者，
但「建立使用者」這個功能本身不是秘密，回 403 讓你知道要去要權限。資源類
（那份文件屬於別的租戶）才回 404，因為連「它存在」都不該洩漏，否則可以拿 id
掃出別人有哪些資源。那條規則等 1B 有資源端點時才落地。

**兩層防線都要驗**：權限判定通過**之後**，資料層的租戶隔離仍然必須生效。
最後一組測試就是在驗這件事——租戶 A 的 Owner 是合法的高權限使用者，
但他不該碰得到租戶 B 的任何東西。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from api.main import create_app
from apps.identity.models import Role
from common.passwords import hash_password
from core.redis import get_redis, tenant_key
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG_A = "tenant-a"
SLUG_B = "tenant-b"

ALLOWED = 200
FORBIDDEN = 403

# 端點 × 角色 → 預期狀態碼。
#
# 只列狀態碼而不驗回應內容：這張表要回答的問題是「擋不擋得住」，內容的正確性
# 由 test_user_endpoints.py 負責。混在一起會讓表變得難讀，而難讀的表沒有人維護。
#
# `owner` / `admin` / `editor` / `viewer` 是 1A-2 種進去的四個系統角色。
PERMISSION_MATRIX = [
    # (method, path, body, 角色 → 預期狀態)
    (
        "GET",
        "/api/v1/users",
        None,
        {"owner": ALLOWED, "admin": ALLOWED, "editor": FORBIDDEN, "viewer": FORBIDDEN},
    ),
    (
        "POST",
        "/api/v1/users",
        {"email": "new@example.com", "display_name": "New", "password": PASSWORD},
        {"owner": 201, "admin": 201, "editor": FORBIDDEN, "viewer": FORBIDDEN},
    ),
    (
        "GET",
        "/api/v1/tenants/current",
        None,
        {"owner": ALLOWED, "admin": ALLOWED, "editor": ALLOWED, "viewer": ALLOWED},
    ),
    (
        "PATCH",
        "/api/v1/tenants/current",
        {"name": "Renamed"},
        {"owner": ALLOWED, "admin": FORBIDDEN, "editor": FORBIDDEN, "viewer": FORBIDDEN},
    ),
    # 個人資料不需要任何權限碼，只要通過認證——每個人都該看得到自己。
    (
        "GET",
        "/api/v1/users/me",
        None,
        {"owner": ALLOWED, "admin": ALLOWED, "editor": ALLOWED, "viewer": ALLOWED},
    ),
]


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    for tenant_id in (TENANT_A, TENANT_B):
        keys = list(client.scan_iter(match=tenant_key(tenant_id, "*")))
        if keys:
            client.delete(*keys)


@pytest.fixture
def users_by_role() -> dict[str, str]:
    """租戶 A 內四個使用者，各自指派一個系統角色；回傳 role → email。

    同步 fixture：ORM 是同步的，在 async 測試函式裡直接建資料會被 Django 擋下。
    """
    ensure_identity_seed()
    emails = {}
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG_A)
        for role_name in ("owner", "admin", "editor", "viewer"):
            email = f"{role_name}@example.com"
            user = make_user(tenant_id=TENANT_A, email=email, password_hash=hash_password(PASSWORD))
            make_user_role(user=user, role=_system_role(role_name))
            emails[role_name] = email
    return emails


def _system_role(name: str) -> Role:
    return Role.objects.get(tenant__isnull=True, name=name)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


async def _token_for(client: httpx.AsyncClient, email: str, *, slug: str = SLUG_A) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": slug, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, f"{email} 登入失敗：{response.text}"
    return str(response.json()["access_token"])


@pytest.mark.parametrize(("method", "path", "body", "expectations"), PERMISSION_MATRIX)
@pytest.mark.parametrize("role", ["owner", "admin", "editor", "viewer"])
async def test_permission_matrix(
    client: httpx.AsyncClient,
    users_by_role: dict[str, str],
    role: str,
    method: str,
    path: str,
    body: dict[str, object] | None,
    expectations: dict[str, int],
) -> None:
    token = await _token_for(client, users_by_role[role])

    response = await client.request(
        method, path, json=body, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == expectations[role], (
        f"{role} 對 {method} {path} 得到 {response.status_code}，"
        f"預期 {expectations[role]}：{response.text[:200]}"
    )


async def test_forbidden_response_uses_the_contract_error_code(
    client: httpx.AsyncClient, users_by_role: dict[str, str]
) -> None:
    """403 的 body 必須是契約裡的 ``PERMISSION_DENIED``（09 附錄 A）。

    client 是依 code 分支的，不是依狀態碼——同樣是 403，「權限不足」與「租戶被
    停權」的處理完全不同（前者去找管理員要權限，後者是要聯絡業務）。
    """
    token = await _token_for(client, users_by_role["viewer"])

    response = await client.get("/api/v1/users", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"


async def test_permission_denial_is_audited(
    client: httpx.AsyncClient,
    users_by_role: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """被拒的權限要留紀錄，且**帶上被拒的 permission code**（10 §3）。

    沒有這筆紀錄的話，「有人在嘗試越權」與「某個角色的權限設錯了」在維運端
    看起來完全一樣——都只是使用者回報「我打不開這個頁面」。
    """
    from config.logging import configure_logging

    configure_logging(level="INFO", fmt="json")
    token = await _token_for(client, users_by_role["viewer"])

    await client.get("/api/v1/users", headers={"Authorization": f"Bearer {token}"})

    assert "user:read" in capsys.readouterr().out


# ── 權限之後，租戶隔離仍然生效 ──────────────────────────────────


@pytest.fixture
def owner_in_tenant_b() -> uuid.UUID:
    ensure_identity_seed()
    with tenant_scope(TENANT_B):
        make_tenant(id=TENANT_B, slug=SLUG_B)
        user = make_user(
            tenant_id=TENANT_B, email="owner-b@example.com", password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=user, role=_system_role("owner"))
    return user.id


async def test_owner_of_one_tenant_cannot_see_users_of_another(
    client: httpx.AsyncClient,
    users_by_role: dict[str, str],
    owner_in_tenant_b: uuid.UUID,
) -> None:
    """兩層防線疊在一起：他有 ``user:read``（第一層過），但只看得到自己租戶的人。

    這條測試的價值在於它會抓到「權限檢查寫對了、但查詢忘了帶租戶」這種組合——
    那時第一層是綠的，而漏掉的是第二層。
    """
    token = await _token_for(client, users_by_role["owner"])

    response = await client.get("/api/v1/users", headers={"Authorization": f"Bearer {token}"})
    emails = {item["email"] for item in response.json()["items"]}

    assert "owner-b@example.com" not in emails
    assert emails == set(users_by_role.values())


async def test_cannot_modify_a_user_of_another_tenant(
    client: httpx.AsyncClient,
    users_by_role: dict[str, str],
    owner_in_tenant_b: uuid.UUID,
) -> None:
    """指名別的租戶的使用者 id 去改——回 404 而不是 403。

    這裡是**資源類**：回 403 等於承認「這個 id 存在，只是你不能碰」，那讓人可以
    拿 id 掃出別的租戶有哪些使用者。回 404 的語意是「在你的世界裡它不存在」，
    而那正是租戶隔離下的事實。
    """
    token = await _token_for(client, users_by_role["owner"])

    response = await client.patch(
        f"/api/v1/users/{owner_in_tenant_b}",
        json={"display_name": "hijacked"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404

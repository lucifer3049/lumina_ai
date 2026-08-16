"""驗收：`/conversations` 的權限與擁有者判定（10 §3、09 §2.4、13 §3 工作包 1D-2）。

**這一包引入了 repo 內第一個「擁有者制」授權**，而它與前面所有的權限檢查是不同的東西：

- `chat:use` 是**角色權限**：這個人能不能用聊天功能。四個角色都有——問答就是這個產品
  本身，Viewer 用不了的話那個角色沒有意義（同 `rag:query` 的理由）。
- **擁有者**是**資源權限**：這場對話是不是他的。09 §2.4 對詳情／修改／刪除標的就是它。

兩者不能互相取代，而混淆的後果很具體：**只檢查 `chat:use` 的話，同租戶的任何人都讀得到
別人的對話。**

**而 RLS 完全擋不住這件事。** RLS 是**租戶級**的隔離——同一個租戶裡的兩個使用者，
policy 看到的 `app.tenant_id` 一模一樣，兩邊的資料互相都在範圍內。前面幾包養成的
「漏寫 filter 也有 RLS 兜底」的直覺，在這裡是錯的：**這一層沒有第二道防線**。

而對話內容比文件更敏感——文件是租戶「放進系統」的東西，對話是「這個人問了什麼」
（誰在查裁員規定、誰在查某個案子）。

**Owner／Admin 也看不到別人的對話**（2026-08-16 產品決策）。稽核需求要另外走審計事件
（2A），而不是讓管理員直接讀對話——後者使用者不會知道。
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
from tests.conftest import TENANT_A
from tests.factories.conversation import make_conversation, make_message
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG_A = "tenant-a"

ALLOWED = 200
CREATED = 201
NO_CONTENT = 204
NOT_FOUND = 404

ROLES = ("owner", "admin", "editor", "viewer")

# 端點 × 角色 → 預期狀態碼。**這張表只回答「角色擋不擋得住」**；擁有者判定在
# `TestOwnership`，因為它與角色無關（每個角色對自己的對話都是通的）。
PERMISSION_MATRIX = [
    ("GET", "/api/v1/conversations", None, dict.fromkeys(ROLES, ALLOWED)),
    (
        "POST",
        "/api/v1/conversations",
        {"title": "新對話"},
        dict.fromkeys(ROLES, CREATED),
    ),
    ("GET", "/api/v1/conversations/{own}", None, dict.fromkeys(ROLES, ALLOWED)),
    ("GET", "/api/v1/conversations/{own}/messages", None, dict.fromkeys(ROLES, ALLOWED)),
    (
        "PATCH",
        "/api/v1/conversations/{own}",
        {"title": "改個名"},
        dict.fromkeys(ROLES, ALLOWED),
    ),
    ("DELETE", "/api/v1/conversations/{own}", None, dict.fromkeys(ROLES, NO_CONTENT)),
]


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    keys = list(client.scan_iter(match=tenant_key(TENANT_A, "*")))
    if keys:
        client.delete(*keys)


@pytest.fixture
def scenario() -> dict[str, object]:
    """租戶 A 內四個角色各一個使用者，每人各有一場自己的對話。

    **每個角色都要有自己的對話**：矩陣驗的是「角色能不能操作自己的東西」，共用一場
    對話的話，Viewer 那一列會因為「不是他的」而 404，看起來像權限不足——而那是兩件
    不同的事。
    """
    ensure_identity_seed()
    emails: dict[str, str] = {}
    own: dict[str, uuid.UUID] = {}
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG_A)
        for role_name in ROLES:
            email = f"{role_name}@example.com"
            user = make_user(tenant_id=TENANT_A, email=email, password_hash=hash_password(PASSWORD))
            make_user_role(user=user, role=Role.objects.get(tenant__isnull=True, name=role_name))
            emails[role_name] = email
            conversation = make_conversation(tenant_id=TENANT_A, user_id=user.id)
            own[role_name] = conversation.id
            # editor 的對話放一則訊息：驗「訊息端點也走同一道擁有者判定」。
            if role_name == "editor":
                make_message(conversation=conversation, content="不該被看到")
    return {"emails": emails, "own": own}


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


async def _token_for(client: httpx.AsyncClient, email: str) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": SLUG_A, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, f"{email} 登入失敗：{response.text}"
    return str(response.json()["access_token"])


@pytest.mark.parametrize(("method", "path", "body", "expected"), PERMISSION_MATRIX)
@pytest.mark.parametrize("role", ROLES)
async def test_permission_matrix(
    client: httpx.AsyncClient,
    scenario: dict[str, object],
    role: str,
    method: str,
    path: str,
    body: dict[str, str] | None,
    expected: dict[str, int],
) -> None:
    emails: dict[str, str] = scenario["emails"]  # type: ignore[assignment]
    own: dict[str, uuid.UUID] = scenario["own"]  # type: ignore[assignment]
    token = await _token_for(client, emails[role])

    response = await client.request(
        method,
        path.replace("{own}", str(own[role])),
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == expected[role], (
        f"{role} {method} {path} → {response.status_code}（預期 {expected[role]}）：{response.text}"
    )


async def test_an_anonymous_request_is_rejected(
    client: httpx.AsyncClient, scenario: dict[str, object]
) -> None:
    response = await client.get("/api/v1/conversations")

    assert response.status_code == 401


class TestOwnership:
    """**本檔的重點。** 同租戶、不同使用者——RLS 在這裡幫不上任何忙。"""

    @pytest.mark.parametrize("intruder", ROLES)
    async def test_nobody_can_read_another_users_conversation(
        self, client: httpx.AsyncClient, scenario: dict[str, object], intruder: str
    ) -> None:
        """**包含 Owner 與 Admin**（2026-08-16 產品決策）。

        對話是「這個人問了什麼」，比文件本身更敏感。稽核需求走審計事件（2A），
        不是讓管理員直接讀——後者使用者不會知道自己被看了。
        """
        emails: dict[str, str] = scenario["emails"]  # type: ignore[assignment]
        own: dict[str, uuid.UUID] = scenario["own"]  # type: ignore[assignment]
        victim = "editor" if intruder != "editor" else "viewer"
        token = await _token_for(client, emails[intruder])

        response = await client.get(
            f"/api/v1/conversations/{own[victim]}",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == NOT_FOUND, (
            f"{intruder} 讀得到 {victim} 的對話（{response.status_code}）"
        )

    async def test_another_users_messages_are_not_readable(
        self, client: httpx.AsyncClient, scenario: dict[str, object]
    ) -> None:
        """訊息端點要走同一道判定。

        對話擋住了但訊息沒擋，等於前門鎖了後門開著——而「知道 conversation id」
        對同租戶的人來說並不困難。
        """
        emails: dict[str, str] = scenario["emails"]  # type: ignore[assignment]
        own: dict[str, uuid.UUID] = scenario["own"]  # type: ignore[assignment]
        token = await _token_for(client, emails["admin"])
        response = await client.get(
            f"/api/v1/conversations/{own['editor']}/messages",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == NOT_FOUND
        assert "不該被看到" not in response.text

    @pytest.mark.parametrize(("method", "body"), [("PATCH", {"title": "改掉"}), ("DELETE", None)])
    async def test_nobody_can_modify_another_users_conversation(
        self,
        client: httpx.AsyncClient,
        scenario: dict[str, object],
        method: str,
        body: dict[str, str] | None,
    ) -> None:
        """讀擋住了、寫沒擋住是更糟的組合：使用者的對話會被別人改名或刪掉，
        而他只會覺得「東西不見了」。"""
        emails: dict[str, str] = scenario["emails"]  # type: ignore[assignment]
        own: dict[str, uuid.UUID] = scenario["own"]  # type: ignore[assignment]
        token = await _token_for(client, emails["owner"])

        response = await client.request(
            method,
            f"/api/v1/conversations/{own['viewer']}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == NOT_FOUND

    async def test_the_list_only_shows_your_own_conversations(
        self, client: httpx.AsyncClient, scenario: dict[str, object]
    ) -> None:
        """列表是擁有者制最容易漏掉的一面。

        詳情端點加了判定、列表忘了加的話，使用者會在自己的列表上看到別人的對話標題
        ——而點進去才 404。標題本身就已經是洩漏了。
        """
        emails: dict[str, str] = scenario["emails"]  # type: ignore[assignment]
        own: dict[str, uuid.UUID] = scenario["own"]  # type: ignore[assignment]
        token = await _token_for(client, emails["owner"])

        response = await client.get(
            "/api/v1/conversations", headers={"Authorization": f"Bearer {token}"}
        )

        ids = {item["id"] for item in response.json()["items"]}
        assert ids == {str(own["owner"])}, ids

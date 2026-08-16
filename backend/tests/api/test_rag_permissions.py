"""驗收：`/rag/query` 的權限矩陣（10 §3、09 §2.3、13 §3 工作包 1C-4）。

型式同 `test_knowledge_permissions.py`：**矩陣式**而不是逐條寫。1C-4 只有一個端點，
矩陣看起來小題大作——但這張表存在的理由不是現在的規模，是「加端點忘了宣告權限會讓
表少一列」。2B 的 rerank 與 1D 的 chat 都會加進來。

**四個角色全部放行**，這是產品決策（2026-08-16）：`rag:query` 是「問問題」，而問問題
就是這個產品本身。Viewer 的定位是「能查、不能改」——查不了的話 Viewer 這個角色沒有
任何意義。它讀得到的東西完全等同 `knowledge:read` 已經給的（同一批文件的內容），
所以不構成新的暴露面。

**分成獨立的權限碼而不是沿用 `knowledge:read`**：檢索每一次都要花錢（embedding 呼叫，
2B 之後還有 rerank），而「能看文件清單」與「能無限次觸發付費呼叫」是兩件事。2A 的
quota 需要一個掛得上去的碼。
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
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.factories.knowledge import make_knowledge_base
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG_A = "tenant-a"

ALLOWED = 200

# 端點 × 角色 → 預期狀態碼。只列狀態碼、不驗內容（內容由 test_rag_endpoints.py 負責）。
PERMISSION_MATRIX = [
    (
        "POST",
        "/api/v1/rag/query",
        {"kb_id": "{kb}", "query": "任何問題"},
        {"owner": ALLOWED, "admin": ALLOWED, "editor": ALLOWED, "viewer": ALLOWED},
    ),
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
    """租戶 A 內四個角色各一個使用者，加一個空的 KB。

    KB 刻意是空的：這張表回答的是「擋不擋得住」，而空 KB 一樣回 200 + 空清單——
    不必為了權限測試去跑一輪 embedding。
    """
    ensure_identity_seed()
    emails: dict[str, str] = {}
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG_A)
        for role_name in ("owner", "admin", "editor", "viewer"):
            email = f"{role_name}@example.com"
            user = make_user(tenant_id=TENANT_A, email=email, password_hash=hash_password(PASSWORD))
            make_user_role(user=user, role=Role.objects.get(tenant__isnull=True, name=role_name))
            emails[role_name] = email
        kb = make_knowledge_base(tenant_id=TENANT_A, name="既有 KB")

    return {"emails": emails, "kb": kb.id}


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
@pytest.mark.parametrize("role", ["owner", "admin", "editor", "viewer"])
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
    kb_id = str(scenario["kb"])
    token = await _token_for(client, emails[role])
    payload = None if body is None else {k: v.replace("{kb}", kb_id) for k, v in body.items()}

    response = await client.request(
        method,
        path.replace("{kb}", kb_id),
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == expected[role], (
        f"{role} {method} {path} → {response.status_code}（預期 {expected[role]}）：{response.text}"
    )


async def test_an_anonymous_request_is_rejected(
    client: httpx.AsyncClient, scenario: dict[str, object]
) -> None:
    """沒有 token 一律 401。

    檢索是付費呼叫的入口——未認證就能打的話，任何人都可以拿它燒別人的額度，
    而帳單上看不出來那些呼叫不是租戶自己發的。
    """
    response = await client.post(
        "/api/v1/rag/query",
        json={"kb_id": str(uuid.uuid4()), "query": "任何問題"},
    )

    assert response.status_code == 401

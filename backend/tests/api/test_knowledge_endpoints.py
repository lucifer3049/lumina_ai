"""驗收：`/knowledge-bases` 與 `/documents` 的行為（09 §2.3、13 §3 工作包 1B-2）。

與 `test_knowledge_permissions.py` 的分工比照 identity 那組：那一檔驗「誰進得來」，
本檔驗「進來之後做的事對不對」。分開的理由是失敗原因完全不同——前者是權限宣告漏
掉，後者是業務邏輯寫錯，混在一起的表沒有人維護。

本檔盯的是四件**做錯了也不會報錯**的事：

1. 刪除是軟刪除，而且刪掉之後**列表與詳情都要看不到它**。只把 `deleted_at` 寫進去
   卻沒有從查詢排除，使用者會看到自己剛刪掉的東西還在。
2. 跨租戶的 id 回 **404 而不是 403**（09 §2.3 資源類規則）——403 等於承認那個 id
   存在，可以拿來掃出別的租戶有哪些 KB。
3. 文件列表以 KB 為範圍。漏了 kb 條件會把整個租戶的文件都列出來，而每一筆都是
   「你有權看」的資料，所以不會有任何錯誤。
4. 回應不夾帶 `storage_key`。那是物件儲存的內部路徑（含 tenant slug 與 kb id），
   外流等於把儲存結構公開，而 1B-3 之後它會是可直接嘗試存取的字串。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from api.main import create_app
from common.passwords import hash_password
from core.db import run_orm
from core.redis import get_redis, tenant_key
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.factories.knowledge import make_document, make_knowledge_base
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG_A = "tenant-a"
SLUG_B = "tenant-b"
OWNER_EMAIL = "owner@example.com"


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    for tenant_id in (TENANT_A, TENANT_B):
        keys = list(client.scan_iter(match=tenant_key(tenant_id, "*")))
        if keys:
            client.delete(*keys)


@pytest.fixture
def tenant_a_with_owner() -> uuid.UUID:
    """租戶 A 與一個 Owner。"""
    ensure_identity_seed()
    from apps.identity.models import Role

    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG_A)
        owner = make_user(
            tenant_id=TENANT_A, email=OWNER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=owner, role=Role.objects.get(tenant__isnull=True, name="owner"))
    return owner.id


@pytest.fixture
def other_tenants_kb() -> dict[str, uuid.UUID]:
    """租戶 B 的 KB 與文件——跨租戶測試的「別人的資源」。"""
    ensure_identity_seed()
    with tenant_scope(TENANT_B):
        make_tenant(id=TENANT_B, slug=SLUG_B)
        kb = make_knowledge_base(tenant_id=TENANT_B, name="租戶 B 的 KB")
        document = make_document(kb=kb, filename="b-secret.pdf")
    return {"kb": kb.id, "document": document.id}


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


async def _token(client: httpx.AsyncClient, email: str = OWNER_EMAIL, slug: str = SLUG_A) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": slug, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_kb(client: httpx.AsyncClient, token: str, name: str = "法規彙編") -> uuid.UUID:
    response = await client.post(
        "/api/v1/knowledge-bases", json={"name": name}, headers=_auth(token)
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


class TestKnowledgeBaseCrud:
    async def test_create_then_read_back(
        self, client: httpx.AsyncClient, tenant_a_with_owner: uuid.UUID
    ) -> None:
        token = await _token(client)

        kb_id = await _create_kb(client, token, name="法規彙編")
        response = await client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "法規彙編"
        # 新建的 KB 是空的——document_count 讓前端不必為了顯示「0 份文件」再打一次。
        assert body["document_count"] == 0

    async def test_list_only_returns_this_tenants_knowledge_bases(
        self,
        client: httpx.AsyncClient,
        tenant_a_with_owner: uuid.UUID,
        other_tenants_kb: dict[str, uuid.UUID],
    ) -> None:
        """列表是隔離的第一個對外表現面。

        兩道防線（Repository filter、RLS）在 1B-1 都驗過了，這裡驗的是端點確實
        走在那條路上——例如某人為了「順便顯示總數」而繞過 Repository 直接查。
        """
        token = await _token(client)
        await _create_kb(client, token, name="我的 KB")

        response = await client.get("/api/v1/knowledge-bases", headers=_auth(token))

        assert response.status_code == 200
        names = {item["name"] for item in response.json()["items"]}
        assert names == {"我的 KB"}
        assert "租戶 B 的 KB" not in names

    async def test_update_changes_only_the_given_fields(
        self, client: httpx.AsyncClient, tenant_a_with_owner: uuid.UUID
    ) -> None:
        """PATCH 的語意是部分更新：沒給的欄位不得被清空。

        用 ``None`` 當「沒給」的哨兵時，最常見的錯誤是把它當成「設為空字串」寫進去
        ——使用者改一次名稱，描述就不見了。
        """
        token = await _token(client)
        kb_id = await _create_kb(client, token)
        await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}",
            json={"description": "公司內規與法遵文件"},
            headers=_auth(token),
        )

        response = await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}", json={"name": "改過的名字"}, headers=_auth(token)
        )

        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "改過的名字"
        assert body["description"] == "公司內規與法遵文件", "沒給的欄位被清空了"

    async def test_deleted_knowledge_base_disappears_from_list_and_detail(
        self, client: httpx.AsyncClient, tenant_a_with_owner: uuid.UUID
    ) -> None:
        """軟刪除之後，列表與詳情都必須看不到它。

        05 §5.4 的軟刪除是為了「使用者可能後悔」（30 天後由清理 job 硬刪），不是
        為了讓資料繼續出現。只寫 ``deleted_at`` 卻沒有從查詢排除的話，使用者會看到
        自己剛刪掉的東西還在列表上——而且刪除 API 回了 204，看起來成功了。
        """
        token = await _token(client)
        kb_id = await _create_kb(client, token)

        assert (
            await client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=_auth(token))
        ).status_code == 204

        detail = await client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=_auth(token))
        listing = await client.get("/api/v1/knowledge-bases", headers=_auth(token))

        assert detail.status_code == 404, "已刪除的 KB 仍查得到詳情"
        assert listing.json()["items"] == [], "已刪除的 KB 仍出現在列表"

    async def test_another_tenants_knowledge_base_is_404_not_403(
        self,
        client: httpx.AsyncClient,
        tenant_a_with_owner: uuid.UUID,
        other_tenants_kb: dict[str, uuid.UUID],
    ) -> None:
        """資源類的跨租戶存取回 **404**（09 §2.3）。

        回 403 等於承認「這個 id 存在，只是你不能碰」——那讓人可以拿 id 逐一嘗試，
        掃出別的租戶有哪些 KB。404 的語意是「在你的世界裡它不存在」，而那正是租戶
        隔離之下的事實。
        """
        token = await _token(client)

        response = await client.get(
            f"/api/v1/knowledge-bases/{other_tenants_kb['kb']}", headers=_auth(token)
        )

        assert response.status_code == 404
        assert response.json()["code"] == "RESOURCE_NOT_FOUND"


class TestDocumentEndpoints:
    async def test_documents_are_listed_per_knowledge_base(
        self, client: httpx.AsyncClient, tenant_a_with_owner: uuid.UUID
    ) -> None:
        """文件列表以 KB 為範圍——不是「這個租戶的全部文件」。

        漏了 kb 條件的話，每一筆回傳的資料都是呼叫者有權看的（同租戶），所以不會有
        任何錯誤或紅燈；使用者只會覺得「這個知識庫怎麼有別的知識庫的文件」。
        """
        token = await _token(client)
        kb_one = await _create_kb(client, token, name="KB 一")
        kb_two = await _create_kb(client, token, name="KB 二")

        # 文件由 factory 直接建（上傳端點屬 1B-3）
        def _seed() -> None:
            with tenant_scope(TENANT_A):
                make_document(kb_id=kb_one, filename="one.pdf")
                make_document(kb_id=kb_two, filename="two.pdf")

        await run_orm(_seed)

        response = await client.get(
            f"/api/v1/knowledge-bases/{kb_one}/documents", headers=_auth(token)
        )

        assert response.status_code == 200
        assert {item["filename"] for item in response.json()["items"]} == {"one.pdf"}

    async def test_document_detail_exposes_etl_status_but_not_storage_key(
        self, client: httpx.AsyncClient, tenant_a_with_owner: uuid.UUID
    ) -> None:
        """詳情要帶 ETL 狀態（09 §2.3），但**不得**帶 ``storage_key``。

        `storage_key` 是物件儲存的內部路徑（`tenant-{slug}/kb/{kb_id}/{doc_id}`）。
        它同時洩漏儲存結構與租戶 slug，而 1B-3 之後那是一個可以直接拿去嘗試存取的
        字串。回應要的是「這份文件處理到哪了」，不是「它存在哪」。
        """
        token = await _token(client)
        kb_id = await _create_kb(client, token)

        def _seed() -> uuid.UUID:
            with tenant_scope(TENANT_A):
                return make_document(kb_id=kb_id, filename="a.pdf").id

        document_id = await run_orm(_seed)

        response = await client.get(f"/api/v1/documents/{document_id}", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "uploaded"
        assert body["doc_version"] == 1
        assert "storage_key" not in body, "物件儲存路徑洩漏到回應"
        assert SLUG_A not in response.text, "租戶 slug 經 storage_key 洩漏"

    async def test_deleted_document_disappears(
        self, client: httpx.AsyncClient, tenant_a_with_owner: uuid.UUID
    ) -> None:
        token = await _token(client)
        kb_id = await _create_kb(client, token)

        def _seed() -> uuid.UUID:
            with tenant_scope(TENANT_A):
                return make_document(kb_id=kb_id, filename="a.pdf").id

        document_id = await run_orm(_seed)

        assert (
            await client.delete(f"/api/v1/documents/{document_id}", headers=_auth(token))
        ).status_code == 204

        detail = await client.get(f"/api/v1/documents/{document_id}", headers=_auth(token))
        listing = await client.get(
            f"/api/v1/knowledge-bases/{kb_id}/documents", headers=_auth(token)
        )

        assert detail.status_code == 404
        assert listing.json()["items"] == []

    async def test_another_tenants_document_is_404(
        self,
        client: httpx.AsyncClient,
        tenant_a_with_owner: uuid.UUID,
        other_tenants_kb: dict[str, uuid.UUID],
    ) -> None:
        token = await _token(client)

        response = await client.get(
            f"/api/v1/documents/{other_tenants_kb['document']}", headers=_auth(token)
        )

        assert response.status_code == 404
        assert "b-secret.pdf" not in response.text, "別的租戶的檔名洩漏到錯誤回應"


class TestValidation:
    async def test_blank_name_is_rejected(
        self, client: httpx.AsyncClient, tenant_a_with_owner: uuid.UUID
    ) -> None:
        """空白名稱回 422（09 附錄 A 的 VALIDATION_FAILED），不是建出一個沒有名字的 KB。"""
        token = await _token(client)

        response = await client.post(
            "/api/v1/knowledge-bases", json={"name": "   "}, headers=_auth(token)
        )

        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_FAILED"

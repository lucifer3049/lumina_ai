"""驗收：reindex 端點（09 §2.3 的 `POST /knowledge-bases/{id}/reindex`，工作包 2B-6）。

09 §2.3 那一列自始就寫著「重嵌入（202 + job）／`knowledge:admin`」，而它至今沒有
實作——`knowledge_version` 從 2B-5 起會遞增，但沒有任何對外的路徑消費它。

這一層要釘住的是**契約**，不是編排（編排在 `tests/integration/test_kb_reindex.py`）：

1. **202 而不是 200**。重建是幾十分鐘的背景批次；回 200 會讓前端以為做完了，而它
   才剛進佇列（同 `documents_reingest` 的理由）。
2. **`knowledge:admin` 而不是 write**。重建一次是整庫重新嵌入的錢，而且期間的檢索
   品質會受影響——破壞範圍等同改整個知識庫的行為，與「上傳一份文件」不是同一級。
3. **進度查得到**。查不到的話，使用者對一個跑了 40 分鐘的東西唯一能做的事就是再
   按一次——而那正是第 4 條要擋的。
4. **重複觸發回 409**。兩個 job 會各自往同一批 chunk 寫不同版本的向量。
5. **稽核**。「誰在什麼時候把整個知識庫重建了」是事後查帳唯一的線索（2A-4）。
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
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.factories.knowledge import (
    make_chunk,
    make_document,
    make_embedding,
    make_knowledge_base,
)
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG_A = "tenant-a"
SLUG_B = "tenant-b"
OWNER_EMAIL = "owner@example.com"
EDITOR_EMAIL = "editor@example.com"
VIEWER_EMAIL = "viewer@example.com"


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    for tenant_id in (TENANT_A, TENANT_B):
        keys = list(client.scan_iter(match=tenant_key(tenant_id, "*")))
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


async def _token(client: httpx.AsyncClient, email: str = OWNER_EMAIL, slug: str = SLUG_A) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": slug, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _kb(tenant_id: uuid.UUID = TENANT_A, *, knowledge_version: int = 1) -> uuid.UUID:
    """一個有內容、可以被重建的 KB。"""
    with tenant_scope(tenant_id):
        kb = make_knowledge_base(
            tenant_id=tenant_id,
            embedding_model="text-embedding-3-small",
            embedding_version=1,
            knowledge_version=knowledge_version,
        )
        document = make_document(kb=kb, status="ready")
        for seq in range(2):
            make_embedding(chunk=make_chunk(document=document, seq=seq))
        return uuid.UUID(str(kb.id))


def _seed_tenant_b() -> None:
    ensure_identity_seed()
    with tenant_scope(TENANT_B):
        make_tenant(id=TENANT_B, slug=SLUG_B)


def _exhaust_token_quota() -> None:
    """把這個租戶當月的 token 計數器推到上限——「已經超額」的狀態。"""
    from services.platform.quota import QuotaService

    service = QuotaService()
    limit = service.limits(TENANT_A)["tokens_month"]
    assert limit is not None, "free plan 應該有 tokens_month 上限"
    service.correct(TENANT_A, "tokens_month", limit)


def _audit_entry() -> Any:
    from apps.platform.models import AuditLog

    with tenant_scope(TENANT_A):
        return AuditLog.objects.filter(action="knowledge_base.reindex").first()


class TestTrigger:
    async def test_it_returns_202_with_the_job(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        kb_id = await run_orm(_kb)

        response = await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/reindex",
            json={"target_model": "gemini-embedding-2"},
            headers=_auth(await _token(client)),
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "pending"
        assert body["target_model"] == "gemini-embedding-2"
        assert body["total_chunks"] == 2
        assert body["embedded_chunks"] == 0
        assert uuid.UUID(body["id"])

    async def test_an_empty_body_means_reindex_with_the_current_model(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """切塊參數改完之後按「重建」——使用者沒有要換模型，body 應該可以是空的。"""
        kb_id = await run_orm(_kb, knowledge_version=2)

        response = await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/reindex",
            json={},
            headers=_auth(await _token(client)),
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["target_model"] == "text-embedding-3-small"
        assert body["rechunk"] is True

    async def test_triggering_twice_conflicts(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """使用者在等 40 分鐘時會再按一次——那是預期行為，不是誤用。"""
        kb_id = await run_orm(_kb)
        token = await _token(client)
        first = await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/reindex", json={}, headers=_auth(token)
        )
        assert first.status_code == 202

        second = await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/reindex", json={}, headers=_auth(token)
        )

        assert second.status_code == 409
        assert second.json()["code"] == "RESOURCE_CONFLICT"

    async def test_an_unknown_kb_is_404(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        response = await client.post(
            f"/api/v1/knowledge-bases/{uuid.uuid4()}/reindex",
            json={},
            headers=_auth(await _token(client)),
        )

        assert response.status_code == 404

    async def test_another_tenants_kb_is_404_not_403(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """403 等於承認那個 id 存在，可以拿來掃出別的租戶有哪些 KB（09 §2.3）。"""
        await run_orm(_seed_tenant_b)
        kb_id = await run_orm(_kb, TENANT_B)

        response = await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/reindex",
            json={},
            headers=_auth(await _token(client)),
        )

        assert response.status_code == 404

    async def test_a_blank_target_model_is_rejected_at_the_edge(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """空字串會照樣落地成一個永遠對不上的 `(model, version)`（1C 的教訓）。"""
        kb_id = await run_orm(_kb)

        response = await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/reindex",
            json={"target_model": "   "},
            headers=_auth(await _token(client)),
        )

        assert response.status_code == 422


class TestQuota:
    """整庫重建是單次花費最大的動作——額度用盡就擋（人類裁決 2026-08-28）。

    這一條驗的是 **HTTP 對映**（429 + `QUOTA_EXCEEDED` + 機器可讀的 details）；
    估算與「檢查不預留」的語意在 `tests/integration/test_kb_reindex_quota.py`。
    """

    async def test_an_exhausted_quota_returns_429(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        kb_id = await run_orm(_kb)
        await run_orm(_exhaust_token_quota)

        response = await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/reindex",
            json={},
            headers=_auth(await _token(client)),
        )

        assert response.status_code == 429, response.text
        body = response.json()
        assert body["code"] == "QUOTA_EXCEEDED"
        # 畫面要說得出「還差多少」，不能只有一句「額度不足」。
        assert body["details"]["resource"] == "tokens_month"
        assert body["details"]["needed"] > 0


class TestPermissions:
    async def test_editor_cannot_trigger_a_reindex(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """Editor 可以上傳文件，但重建整庫是維運等級的動作（本檔第 2 條）。"""
        kb_id = await run_orm(_kb)

        response = await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/reindex",
            json={},
            headers=_auth(await _token(client, EDITOR_EMAIL)),
        )

        assert response.status_code == 403

    async def test_viewer_can_read_the_progress(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """看進度是 `knowledge:read`：問不到東西的人要看得出「正在重建」。"""
        kb_id = await run_orm(_kb)
        await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/reindex",
            json={},
            headers=_auth(await _token(client)),
        )

        response = await client.get(
            f"/api/v1/knowledge-bases/{kb_id}/reindex",
            headers=_auth(await _token(client, VIEWER_EMAIL)),
        )

        assert response.status_code == 200

    async def test_it_requires_authentication(self, client: httpx.AsyncClient) -> None:
        response = await client.post(f"/api/v1/knowledge-bases/{uuid.uuid4()}/reindex", json={})

        assert response.status_code == 401


class TestProgressEndpoint:
    async def test_it_returns_the_latest_job(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        kb_id = await run_orm(_kb)
        token = await _token(client)
        started = await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/reindex", json={}, headers=_auth(token)
        )

        response = await client.get(
            f"/api/v1/knowledge-bases/{kb_id}/reindex", headers=_auth(token)
        )

        assert response.status_code == 200
        assert response.json()["id"] == started.json()["id"]

    async def test_a_kb_that_was_never_reindexed_is_404(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """回 200 加一個空殼的話，前端分不出「沒跑過」與「跑完了」。"""
        kb_id = await run_orm(_kb)

        response = await client.get(
            f"/api/v1/knowledge-bases/{kb_id}/reindex", headers=_auth(await _token(client))
        )

        assert response.status_code == 404


class TestNeedsReindexOnTheKnowledgeBase:
    """2B-5 的 `knowledge_version` 到這裡才有使用者看得見的出口。"""

    async def test_a_fresh_kb_does_not_need_one(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        kb_id = await run_orm(_kb)

        response = await client.get(
            f"/api/v1/knowledge-bases/{kb_id}", headers=_auth(await _token(client))
        )

        assert response.json()["needs_reindex"] is False

    async def test_changing_the_chunk_config_surfaces_it(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """沒有這一欄，使用者改完切塊參數之後看到的是一個「已儲存」的成功訊息，
        而既有 chunk 全部還是用舊參數切的——那個落差沒有任何地方顯示。"""
        kb_id = await run_orm(_kb)
        token = await _token(client)
        patched = await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}",
            json={"config": {"chunk": {"target_tokens": 256}}},
            headers=_auth(token),
        )
        assert patched.status_code == 200, patched.text

        response = await client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=_auth(token))

        assert response.json()["needs_reindex"] is True


class TestAudit:
    async def test_a_reindex_is_audited(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """整庫重建要花錢也會影響所有人的答案——2A-4 的稽核清單該有這一條。"""
        kb_id = await run_orm(_kb)
        await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/reindex",
            json={"target_model": "gemini-embedding-2"},
            headers=_auth(await _token(client)),
        )

        entry: Any = await run_orm(_audit_entry)
        assert entry is not None, "重建沒有留下稽核紀錄"
        assert str(entry.resource_id) == str(kb_id)


class TestContractStability:
    def test_the_operation_ids_are_declared(self) -> None:
        """operation_id 的穩定性視同 API 契約（CLAUDE.md）——前端的 codegen 吃它。"""
        schema = create_app().openapi()
        operations = {
            operation["operationId"]
            for path in schema["paths"].values()
            for operation in path.values()
            if isinstance(operation, dict) and "operationId" in operation
        }

        assert "knowledge_bases_reindex" in operations
        assert "knowledge_bases_reindex_status" in operations

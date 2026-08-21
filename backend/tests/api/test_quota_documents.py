"""驗收：上傳路徑的配額強制執行——文件數與儲存量（04 §8.1，2A-2a）。

這兩種資源與 token/訊息不同：它們是**存量**不是流量，且必須活得比 Redis 久
（文件躺在 DB 與物件儲存裡，計數器 flush 掉不代表容量回來了）。因此擋線的依據
是 DB 聚合（active 文件的 COUNT 與 size_bytes 的 SUM），Redis 不參與——刪除
文件容量自然回來，沒有「計數器與現實漂移」這種故障。

三件事錯了都不會有例外：

1. **擋在寫入之後**。429 了但文件已建立／位元組已進物件儲存——容量繼續被吃，
   而使用者看到的是失敗。
2. **軟刪除的文件繼續占額度**。使用者刪了東西卻騰不出空間，唯一的解法變成
   「聯絡客服」。（注意：物件儲存的位元組要等清理 job 才真的消失——額度放的是
   **邏輯**容量，那是使用者能控制的東西。）
3. **32MB 單檔上限（1B-3 的 413）與儲存配額（429）混為一談**。前者保護的是
   單一請求的解析資源、永遠適用；後者是租戶的商務額度、可談可調。
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
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.factories.knowledge import make_knowledge_base
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG = "tenant-a"
OWNER_EMAIL = "owner@example.com"

_MARKDOWN = "# 測試文件\n\n配額測試用的內容。\n".encode()


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    keys = list(client.scan_iter(match=tenant_key(TENANT_A, "*")))
    if keys:
        client.delete(*keys)


def _owner_with_quota(quota: dict[str, Any]) -> uuid.UUID:
    from apps.identity.models import Role

    ensure_identity_seed()
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG, settings={"quota": quota})
        user = make_user(
            tenant_id=TENANT_A, email=OWNER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=user, role=Role.objects.get(tenant__isnull=True, name="owner"))
    return uuid.UUID(str(user.id))


def _kb() -> uuid.UUID:
    with tenant_scope(TENANT_A):
        kb = make_knowledge_base(tenant_id=TENANT_A)
    return uuid.UUID(str(kb.id))


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": SLUG, "email": OWNER_EMAIL, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _upload(
    client: httpx.AsyncClient, token: str, kb_id: uuid.UUID, *, content: bytes = _MARKDOWN
) -> httpx.Response:
    return await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        headers=_auth(token),
        files={"file": ("quota.md", content, "text/markdown")},
    )


class TestDocumentCount:
    async def test_the_limit_blocks_the_next_upload(self, client: httpx.AsyncClient) -> None:
        await run_orm(_owner_with_quota, {"documents": 1})
        kb_id = await run_orm(_kb)
        token = await _token(client)
        first = await _upload(client, token, kb_id)
        assert first.status_code == 201, first.text

        blocked = await _upload(client, token, kb_id, content="# 第二份\n\n不同內容。\n".encode())

        assert blocked.status_code == 429, blocked.text
        body = blocked.json()
        assert body["code"] == "QUOTA_EXCEEDED"
        assert body["details"]["resource"] == "documents"

    async def test_a_blocked_upload_leaves_no_document(self, client: httpx.AsyncClient) -> None:
        await run_orm(_owner_with_quota, {"documents": 1})
        kb_id = await run_orm(_kb)
        token = await _token(client)
        await _upload(client, token, kb_id)

        await _upload(
            client, token, kb_id, content="# 第二份\n\n不同內容。\n".encode()
        )  # 被擋的那一次

        listed = await client.get(
            f"/api/v1/knowledge-bases/{kb_id}/documents", headers=_auth(token)
        )
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == 1

    async def test_deleting_frees_the_slot(self, client: httpx.AsyncClient) -> None:
        """額度看的是 active 文件——刪掉一份就能再上傳，不需要任何對帳。"""
        await run_orm(_owner_with_quota, {"documents": 1})
        kb_id = await run_orm(_kb)
        token = await _token(client)
        first = await _upload(client, token, kb_id)
        document_id = first.json()["id"]

        deleted = await client.delete(f"/api/v1/documents/{document_id}", headers=_auth(token))
        assert deleted.status_code in (200, 204), deleted.text

        # 換一份內容：同內容重傳會先撞上 1B-3 的去重（409），驗不到配額這一層。
        again = await _upload(client, token, kb_id, content="# 另一份\n\n新內容。\n".encode())
        assert again.status_code == 201, again.text


class TestStorageBytes:
    async def test_the_limit_blocks_before_anything_is_written(
        self, client: httpx.AsyncClient
    ) -> None:
        """單檔合法（遠小於 32MB 的 413 線）但會讓總量超標 → 429，且什麼都沒寫。"""
        await run_orm(_owner_with_quota, {"storage_bytes": len(_MARKDOWN) + 10})
        kb_id = await run_orm(_kb)
        token = await _token(client)
        first = await _upload(client, token, kb_id)
        assert first.status_code == 201, first.text

        blocked = await _upload(
            client, token, kb_id, content="# 第二份\n\n夠長的不同內容，總量會超標。\n".encode()
        )

        assert blocked.status_code == 429, blocked.text
        assert blocked.json()["details"]["resource"] == "storage_bytes"
        listed = await client.get(
            f"/api/v1/knowledge-bases/{kb_id}/documents", headers=_auth(token)
        )
        assert len(listed.json()["items"]) == 1

"""驗收：KB 設定的讀寫端點（09 §2.3 的 `PATCH /knowledge-bases/{id}`，工作包 2B-5）。

09 §2.3 那一列自始就寫著 PATCH 管的是「詳情 / **設定（chunk、檢索參數）** / 刪除」，
但 1B-2 只做了 name 與 description——`config` 這一欄從來沒有對外的寫入路徑。於是
15 §4.1 的三層覆寫（系統預設 → 租戶 → KB）第三層雖然讀得到，卻沒有人填得進去。

與 `test_knowledge_endpoints.py` 的分工：那一檔驗 CRUD 的語意（軟刪除、跨租戶 404、
部分更新），本檔只驗 `config` 這一欄——它有四個自己的陷阱：

1. **填錯不擋**。存得進去、讀得回來、設定畫面看得見，只是永遠不生效
   （15 §4.1 的「後台改了沒有反應」）。
2. **權限給錯**。改檢索參數會改變**所有人**問到的答案，破壞範圍等同改整個知識庫的
   行為——它與「上傳一份文件」不是同一個等級（09 §2.3 的 `knowledge:admin`）。
3. **部分更新把 config 清空**。使用者改一次名字，整組調過的參數就沒了，而 API 回
   200 看起來完全成功。
4. **改了切塊參數卻不遞增 `knowledge_version`**。05 §81 的那一欄的用途就是「這個 KB
   需要重建嗎」，而 2B-6 的 reindex 靠它判定。不遞增的話，使用者改了 chunk 大小、
   系統卻認為既有的 chunk 仍然有效——那些 chunk 是用舊參數切的，而沒有任何地方
   看得出來。
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
from tests.factories.knowledge import make_knowledge_base
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG_A = "tenant-a"
SLUG_B = "tenant-b"
OWNER_EMAIL = "owner@example.com"
EDITOR_EMAIL = "editor@example.com"


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
    """一個 Owner（有 `knowledge:admin`）與一個 Editor（只有 `knowledge:write`）。"""
    ensure_identity_seed()
    from apps.identity.models import Role

    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG_A)
        owner = make_user(
            tenant_id=TENANT_A, email=OWNER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=owner, role=Role.objects.get(tenant__isnull=True, name="owner"))
        editor = make_user(
            tenant_id=TENANT_A, email=EDITOR_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=editor, role=Role.objects.get(tenant__isnull=True, name="editor"))


@pytest.fixture
def other_tenants_kb() -> uuid.UUID:
    ensure_identity_seed()
    with tenant_scope(TENANT_B):
        make_tenant(id=TENANT_B, slug=SLUG_B)
        kb = make_knowledge_base(tenant_id=TENANT_B, name="租戶 B 的 KB")
    return uuid.UUID(str(kb.id))


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


async def _create_kb(client: httpx.AsyncClient, token: str, **body: object) -> uuid.UUID:
    response = await client.post(
        "/api/v1/knowledge-bases", json={"name": "法規彙編", **body}, headers=_auth(token)
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


def _stored(kb_id: uuid.UUID) -> Any:
    from repositories.knowledge import KnowledgeBaseRepository

    with tenant_scope(TENANT_A):
        return KnowledgeBaseRepository().get_by_id(kb_id)


class TestReadBack:
    async def test_config_is_part_of_the_representation(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """**設定畫面要看得到現在存了什麼**（15 §4.1 的統一設定畫面，2C）。

        只能寫不能讀的話，前端要嘛自己記一份（那會與 DB 漂），要嘛每次都顯示空白
        ——而空白與「沒有覆寫」在畫面上長得一樣。
        """
        kb_id = await _create_kb(client, await _token(client))

        response = await client.get(
            f"/api/v1/knowledge-bases/{kb_id}", headers=_auth(await _token(client))
        )

        assert response.status_code == 200
        assert response.json()["config"] == {}

    async def test_create_accepts_a_config(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        token = await _token(client)

        kb_id = await _create_kb(client, token, config={"retrieval": {"top_k": 20}})

        response = await client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=_auth(token))
        assert response.json()["config"] == {"retrieval": {"top_k": 20}}


class TestWrite:
    async def test_patch_stores_the_config(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        token = await _token(client)
        kb_id = await _create_kb(client, token)

        response = await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}",
            json={"config": {"retrieval": {"top_k": 12, "retrieval_mode": "hybrid+rerank"}}},
            headers=_auth(token),
        )

        assert response.status_code == 200, response.text
        assert response.json()["config"]["retrieval"]["top_k"] == 12
        stored = await run_orm(_stored, kb_id)
        assert stored.config["retrieval"]["retrieval_mode"] == "hybrid+rerank"

    async def test_the_stored_config_actually_takes_effect(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """寫進去的值要真的被檢索讀到——**這是整條寫入路徑存在的唯一理由**。

        端點驗過了、DB 有了、而讀取端讀的是另一個區塊名或另一個鍵名的話，使用者會
        看到「設定有存起來」但「答案完全沒變」，那正是 15 §4.1 要防的症狀。
        """
        from services.rag.retrieval import RetrievalService

        token = await _token(client)
        kb_id = await _create_kb(client, token)
        await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}",
            json={"config": {"retrieval": {"top_k": 7}}},
            headers=_auth(token),
        )

        params = await run_orm(RetrievalService().params_for, TENANT_A, [kb_id])

        assert params.top_k == 7

    async def test_patch_without_config_leaves_it_untouched(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """部分更新：``None`` 是「這次沒給」，不是「設為空」。

        混淆的症狀是使用者改一次名字，整組調過的參數就回到系統預設——而 API 回 200。
        """
        token = await _token(client)
        kb_id = await _create_kb(client, token, config={"retrieval": {"top_k": 12}})

        response = await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}", json={"name": "改過的名字"}, headers=_auth(token)
        )

        assert response.status_code == 200
        assert response.json()["config"] == {"retrieval": {"top_k": 12}}

    async def test_an_empty_object_clears_the_overrides(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """``{}`` 是明確的「清空、回到系統預設」——與「沒給」必須分得開，否則使用者
        沒有辦法把一個調壞的 KB 還原。"""
        token = await _token(client)
        kb_id = await _create_kb(client, token, config={"retrieval": {"top_k": 12}})

        response = await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}", json={"config": {}}, headers=_auth(token)
        )

        assert response.status_code == 200
        assert response.json()["config"] == {}

    async def test_config_replaces_wholesale_not_deep_merges(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """給了 `config` 就是**整份取代**，不是逐鍵合併。

        深層合併讀起來比較體貼，但它讓「刪掉一個覆寫」變成不可能——送什麼都只會再
        加上去，而使用者唯一的出路是把整個 KB 刪掉重建。取代的語意下，前端送的是
        它畫面上完整的那一份，而那本來就是設定畫面手上有的東西。
        """
        token = await _token(client)
        kb_id = await _create_kb(
            client, token, config={"retrieval": {"top_k": 12}, "chunk": {"target_tokens": 800}}
        )

        response = await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}",
            json={"config": {"retrieval": {"top_k": 12}}},
            headers=_auth(token),
        )

        assert response.json()["config"] == {"retrieval": {"top_k": 12}}


class TestValidation:
    async def test_a_misspelled_section_is_rejected(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """422 + `errors[]`（09 §1.3 / 附錄 A）。**不是 200**：存得進去而永遠不生效
        的設定，比擋下來難查一百倍。"""
        token = await _token(client)
        kb_id = await _create_kb(client, token)

        response = await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}",
            json={"config": {"retreival": {"top_k": 10}}},
            headers=_auth(token),
        )

        assert response.status_code == 422, response.text
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert body["code"] == "VALIDATION_FAILED"
        assert [item["field"] for item in body["errors"]] == ["config.retreival"]

    async def test_every_offending_key_comes_back_at_once(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """一次只回一個錯的話，使用者要來回試五次——而每一次他都以為只剩最後一個。"""
        token = await _token(client)
        kb_id = await _create_kb(client, token)

        response = await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}",
            json={"config": {"retrieval": {"top_k": 0, "retrieval_mode": "magic"}}},
            headers=_auth(token),
        )

        assert response.status_code == 422
        assert len(response.json()["errors"]) == 2

    async def test_a_rejected_config_does_not_touch_the_stored_one(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """422 之後 DB 要維持原狀——半套寫入（name 進去了、config 被擋下）會讓
        使用者收到錯誤訊息，卻不知道另一半已經改掉了。"""
        token = await _token(client)
        kb_id = await _create_kb(client, token, config={"retrieval": {"top_k": 12}})

        await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}",
            json={"name": "新名字", "config": {"retrieval": {"top_k": 0}}},
            headers=_auth(token),
        )

        response = await client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=_auth(token))
        body = response.json()
        assert body["config"] == {"retrieval": {"top_k": 12}}
        assert body["name"] == "法規彙編", "config 被擋下，name 卻寫進去了"

    async def test_create_validates_the_same_way(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """建立與更新走同一條驗證。分開寫的話，其中一條遲早會漏掉新加的參數，而
        使用者會發現「建立時可以填、修改時不行」（或反過來）。"""
        token = await _token(client)

        response = await client.post(
            "/api/v1/knowledge-bases",
            json={"name": "新 KB", "config": {"chunk": {"target_tokens": "五百"}}},
            headers=_auth(token),
        )

        assert response.status_code == 422, response.text
        assert [item["field"] for item in response.json()["errors"]] == [
            "config.chunk.target_tokens"
        ]


class TestPermissions:
    async def test_editor_cannot_change_the_config(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """改檢索參數改變的是**所有人**問到的答案，破壞範圍等同改整個知識庫的行為
        ——與「上傳一份文件」不是同一個等級（09 §2.3、api/v1/knowledge.py 的分級）。"""
        owner_token = await _token(client)
        kb_id = await _create_kb(client, owner_token)
        editor_token = await _token(client, EDITOR_EMAIL)

        response = await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}",
            json={"config": {"retrieval": {"top_k": 12}}},
            headers=_auth(editor_token),
        )

        assert response.status_code == 403, response.text

    async def test_another_tenants_kb_is_404_not_403(
        self,
        client: httpx.AsyncClient,
        tenant_a_with_roles: None,
        other_tenants_kb: uuid.UUID,
    ) -> None:
        """403 等於承認那個 id 存在（09 §2.3 資源類規則）。"""
        response = await client.patch(
            f"/api/v1/knowledge-bases/{other_tenants_kb}",
            json={"config": {"retrieval": {"top_k": 12}}},
            headers=_auth(await _token(client)),
        )

        assert response.status_code == 404


class TestKnowledgeVersion:
    async def test_changing_the_chunk_section_bumps_the_knowledge_version(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """05 §81：`knowledge_version` 是「設定變更遞增」，用途是「這個 KB 需要重建
        嗎」。切塊參數變了，既有的 chunk 就是用舊參數切的——不遞增的話，2B-6 的
        reindex 判定會說「不必重建」，而那些 chunk 與新設定完全不符。
        """
        token = await _token(client)
        kb_id = await _create_kb(client, token)
        before = (await run_orm(_stored, kb_id)).knowledge_version

        await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}",
            json={"config": {"chunk": {"target_tokens": 800}}},
            headers=_auth(token),
        )

        assert (await run_orm(_stored, kb_id)).knowledge_version == before + 1

    async def test_changing_only_retrieval_params_does_not_bump_it(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """檢索參數是**讀路徑**的旋鈕，改它不影響任何已經存在的 chunk。

        跟著遞增的話，每一次微調 top_k 都會讓那個 KB 看起來「需要重建」——而重建
        一次要把整庫重新嵌入，是真的錢。
        """
        token = await _token(client)
        kb_id = await _create_kb(client, token)
        before = (await run_orm(_stored, kb_id)).knowledge_version

        await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}",
            json={"config": {"retrieval": {"top_k": 12}}},
            headers=_auth(token),
        )

        assert (await run_orm(_stored, kb_id)).knowledge_version == before

    async def test_rewriting_the_same_chunk_values_does_not_bump_it(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """比的是「值變了沒有」，不是「這次有沒有送 chunk 區」。

        設定畫面每次儲存都會把整份 config 送回來（見 `TestWrite` 的整份取代），所以
        「有送就遞增」等於「每按一次儲存就要求重建一次整個知識庫」。
        """
        token = await _token(client)
        kb_id = await _create_kb(client, token, config={"chunk": {"target_tokens": 800}})
        before = (await run_orm(_stored, kb_id)).knowledge_version

        await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}",
            json={"config": {"chunk": {"target_tokens": 800}}},
            headers=_auth(token),
        )

        assert (await run_orm(_stored, kb_id)).knowledge_version == before


class TestAudit:
    async def test_a_config_change_records_before_and_after(
        self, client: httpx.AsyncClient, tenant_a_with_roles: None
    ) -> None:
        """`knowledge_bases_update` 本來就在稽核清單裡（api/middleware/audit.py），
        但沒有 before/after 的話那一列只說得出「有人改過設定」。

        改檢索參數會讓**所有人**的答案變差，而症狀（「最近答得怪怪的」）與設定變更
        之間隔著幾天——那時唯一查得到「誰、什麼時候、從什麼改成什麼」的地方就是這裡。
        """
        from repositories.platform import AuditLogRepository

        token = await _token(client)
        kb_id = await _create_kb(client, token, config={"retrieval": {"top_k": 12}})
        await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}",
            json={"config": {"retrieval": {"top_k": 40}}},
            headers=_auth(token),
        )

        def _latest() -> Any:
            with tenant_scope(TENANT_A):
                return (
                    AuditLogRepository()
                    .get_queryset()
                    .filter(action="knowledge_base.update")
                    .order_by("-created_at")
                    .first()
                )

        row = await run_orm(_latest)
        assert row is not None
        assert row.before["config"] == {"retrieval": {"top_k": 12}}
        assert row.after["config"] == {"retrieval": {"top_k": 40}}

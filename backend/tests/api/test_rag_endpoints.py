"""驗收：`POST /rag/query`（09 §2.3 的獨立檢索 API、13 §3 工作包 1C-4）。

這個端點**不生成答案**，只回「找到哪些 chunk、分數多少」。它存在的理由有兩個，而
兩個都與 1D 無關：整合方需要一個純檢索介面；而在 Chat 做完之前，這是唯一能實際看到
「檢索到底準不準」的入口——沒有它，檢索品質要等一整個工作包之後才第一次被人看見。

分工比照 knowledge 那組：本檔驗「進來之後回的東西對不對」，權限矩陣在
`test_rag_permissions.py`。

三件事做錯了不會報錯：

1. **跨租戶的 kb_id 要回 404 而不是 403**（09 §2.3）。403 等於承認那個 id 存在，
   可以拿來掃出別的租戶有哪些知識庫。
2. **回應不得夾帶內部欄位**。chunk 的 `tenant_id`、文件的 `storage_key` 都不該出去
   ——後者是物件儲存的實際路徑。
3. **`operation_id` 是 API 契約的一部分**（CLAUDE.md）：它決定前端 codegen 產出的
   函式名稱，改名等於改前端的呼叫端，而後端測試不會有任何反應。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from ai.gateway import AIGateway
from ai.gateway.providers.mock import MockEmbeddingProvider
from api.main import create_app
from common.passwords import hash_password
from core.redis import get_redis, tenant_key
from services.knowledge.embedding import EmbeddingService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.factories.knowledge import make_chunk, make_document, make_knowledge_base
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG_A = "tenant-a"
SLUG_B = "tenant-b"
OWNER_EMAIL = "owner@example.com"

_CONTENT = "員工請假應於三日前提出申請"


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
    ensure_identity_seed()
    from apps.identity.models import Role

    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG_A)
        owner = make_user(
            tenant_id=TENANT_A, email=OWNER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=owner, role=Role.objects.get(tenant__isnull=True, name="owner"))
    return owner.id


def _seed_kb(tenant_id: uuid.UUID) -> uuid.UUID:
    """一個有向量的 KB。走真的 `EmbeddingService`，理由見 test_vector_retrieval.py。"""
    with tenant_scope(tenant_id):
        kb = make_knowledge_base(tenant_id=tenant_id)
        document = make_document(kb=kb, status="chunked")
        make_chunk(
            document=document,
            seq=0,
            content=_CONTENT,
            meta={"page": 7, "heading_path": ["人事規章", "請假"]},
        )
    EmbeddingService(
        gateway=AIGateway(embedding_provider=MockEmbeddingProvider(), retry_backoff_seconds=())
    ).embed_document(tenant_id, document.id)
    return uuid.UUID(str(kb.id))


@pytest.fixture
def seeded_kb(tenant_a_with_owner: uuid.UUID) -> uuid.UUID:
    """租戶 A 的一個 KB，內含一個已算好向量的 chunk。

    **同步 fixture**：Django ORM 是同步的，在 async 測試函式裡直接建資料會被
    `SynchronousOnlyOperation` 擋下（同 test_knowledge_permissions.py 的 scenario）。
    """
    return _seed_kb(TENANT_A)


@pytest.fixture
def empty_kb(tenant_a_with_owner: uuid.UUID) -> uuid.UUID:
    with tenant_scope(TENANT_A):
        kb = make_knowledge_base(tenant_id=TENANT_A)
    return uuid.UUID(str(kb.id))


@pytest.fixture
def other_tenants_kb() -> uuid.UUID:
    ensure_identity_seed()
    with tenant_scope(TENANT_B):
        make_tenant(id=TENANT_B, slug=SLUG_B)
    return _seed_kb(TENANT_B)


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


class TestQuery:
    async def test_it_returns_the_matching_chunk_with_a_score(
        self, client: httpx.AsyncClient, seeded_kb: uuid.UUID
    ) -> None:
        token = await _token(client)
        kb_id = seeded_kb

        response = await client.post(
            "/api/v1/rag/query",
            json={"kb_id": str(kb_id), "query": _CONTENT},
            headers=_auth(token),
        )

        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["content"] == _CONTENT
        # **2B-2 起 `score` 是 RRF 的融合分數**（名次倒數和，第一名 1/61），不再是
        # 餘弦相似度。只保證「越大越相關」，尺度本身沒有意義——因此這裡驗的是正數
        # 與排序，不是絕對值。餘弦的性質改由 `test_vector_retrieval.py` 在 repository
        # 那一層守。09 §2.3 的欄位說明待同步（2B 結案時一併改，含 `pnpm gen:api`）。
        assert items[0]["score"] > 0

    async def test_the_response_carries_what_a_citation_needs(
        self, client: httpx.AsyncClient, seeded_kb: uuid.UUID
    ) -> None:
        """整合方拿到的東西要足以自己組出「出自哪份文件第幾頁」。

        少了這些欄位，這個端點就只是「回一堆文字」——而呼叫端無從得知那些文字
        是哪來的，也就無法驗證答案。
        """
        token = await _token(client)
        kb_id = seeded_kb

        response = await client.post(
            "/api/v1/rag/query",
            json={"kb_id": str(kb_id), "query": _CONTENT},
            headers=_auth(token),
        )

        item = response.json()["items"][0]
        assert uuid.UUID(item["chunk_id"])
        assert uuid.UUID(item["document_id"])
        assert item["page"] == 7
        assert item["heading_path"] == ["人事規章", "請假"]

    async def test_internal_fields_are_not_leaked(
        self, client: httpx.AsyncClient, seeded_kb: uuid.UUID
    ) -> None:
        """`storage_key` 是物件儲存的實際路徑；`tenant_id` 是 client 不該看到的東西
        （鐵則 4：不接受也不回傳 client 自報的租戶）。"""
        token = await _token(client)
        kb_id = seeded_kb

        response = await client.post(
            "/api/v1/rag/query",
            json={"kb_id": str(kb_id), "query": _CONTENT},
            headers=_auth(token),
        )

        body = response.text
        assert "storage_key" not in body
        assert "tenant_id" not in body

    async def test_top_k_is_accepted(self, client: httpx.AsyncClient, seeded_kb: uuid.UUID) -> None:
        token = await _token(client)
        kb_id = seeded_kb

        response = await client.post(
            "/api/v1/rag/query",
            json={"kb_id": str(kb_id), "query": _CONTENT, "top_k": 1},
            headers=_auth(token),
        )

        assert response.status_code == 200
        assert len(response.json()["items"]) == 1

    async def test_an_empty_knowledge_base_returns_an_empty_list(
        self, client: httpx.AsyncClient, empty_kb: uuid.UUID
    ) -> None:
        """查不到東西是 200 + 空清單，不是 404。

        404 的意思是「這個 KB 不存在」，而「這個 KB 存在但沒有相關內容」是完全不同
        的情況——1D 對後者有自己的處置（回「知識庫無相關內容」）。
        """
        token = await _token(client)

        response = await client.post(
            "/api/v1/rag/query",
            json={"kb_id": str(empty_kb), "query": "任何問題"},
            headers=_auth(token),
        )

        assert response.status_code == 200
        assert response.json()["items"] == []


class TestValidation:
    @pytest.mark.parametrize("query", ["", "   "])
    async def test_a_blank_query_is_rejected(
        self, client: httpx.AsyncClient, seeded_kb: uuid.UUID, query: str
    ) -> None:
        """空查詢不該送去算 embedding（理由見 test_vector_retrieval.py）。"""
        token = await _token(client)
        kb_id = seeded_kb

        response = await client.post(
            "/api/v1/rag/query",
            json={"kb_id": str(kb_id), "query": query},
            headers=_auth(token),
        )

        assert response.status_code == 422, response.text

    @pytest.mark.parametrize("top_k", [0, -1, 500])
    async def test_out_of_range_top_k_is_rejected(
        self, client: httpx.AsyncClient, seeded_kb: uuid.UUID, top_k: int
    ) -> None:
        """上限是保護 DB 的：`top_k` 直接進 SQL 的 LIMIT，而呼叫端是外部整合方。

        沒有上限的話，一個 `top_k=1000000` 的請求會讓 pgvector 把整個 KB 的向量掃出來
        排序——那不會失敗，只會讓那台 DB 在那幾秒內對所有租戶都很慢。
        """
        token = await _token(client)
        kb_id = seeded_kb

        response = await client.post(
            "/api/v1/rag/query",
            json={"kb_id": str(kb_id), "query": _CONTENT, "top_k": top_k},
            headers=_auth(token),
        )

        assert response.status_code == 422, response.text


class TestTenantIsolation:
    async def test_another_tenants_kb_is_not_found(
        self,
        client: httpx.AsyncClient,
        tenant_a_with_owner: uuid.UUID,
        other_tenants_kb: uuid.UUID,
    ) -> None:
        """404 而不是 403（09 §2.3）：403 等於承認那個 id 存在。"""
        token = await _token(client)

        response = await client.post(
            "/api/v1/rag/query",
            json={"kb_id": str(other_tenants_kb), "query": _CONTENT},
            headers=_auth(token),
        )

        assert response.status_code == 404, response.text

    async def test_an_unknown_kb_is_also_404(
        self, client: httpx.AsyncClient, seeded_kb: uuid.UUID
    ) -> None:
        """不存在與別人的，對外必須無法區分——否則 404/403 的差異本身就是情報。"""
        token = await _token(client)

        response = await client.post(
            "/api/v1/rag/query",
            json={"kb_id": str(uuid.uuid4()), "query": _CONTENT},
            headers=_auth(token),
        )

        assert response.status_code == 404


class TestContract:
    def test_the_operation_id_is_stable(self) -> None:
        """`operation_id` 決定前端 codegen 出來的函式名（CLAUDE.md 鐵則 10）。

        改它等於改前端的呼叫端，而後端測試不會有任何反應——症狀是 `pnpm gen:api`
        之後前端整包編不過，而錯誤訊息指向一個「不存在的函式」。
        """
        schema = create_app().openapi()

        assert schema["paths"]["/api/v1/rag/query"]["post"]["operationId"] == "rag_query"

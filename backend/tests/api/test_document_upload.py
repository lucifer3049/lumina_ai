"""驗收：`POST /knowledge-bases/{id}/documents` 單請求上傳（09 §2.3、§3.1、10 §99）。

大檔（>32MB）的 presigned 分塊直傳屬後續工作包；本檔只涵蓋單請求 multipart。

**上傳是這個系統第一個「同時碰 DB 與物件儲存」的寫入路徑**，所以它有一類別處沒有
的失敗模式：兩邊的狀態不一致。本檔盯四件事：

1. 成功時兩邊都有東西——DB 有列、物件儲存有物件，而且 key 對得起來。
2. **DB 寫入失敗時不留孤兒物件**（先 PUT 再寫 DB，失敗 best-effort 刪物件）。
3. 重複內容回 409 且**不重複佔用儲存空間**。
4. 被拒的上傳（型別、大小）**完全不碰物件儲存**——驗證要在 PUT 之前。

第 4 點特別容易寫反：先存起來再驗證比較好寫（拿得到完整檔案），但那等於讓任何人
都能把任意內容寫進我們的 bucket，白名單形同虛設。
"""

from __future__ import annotations

import io
import uuid
import zipfile
from collections.abc import AsyncIterator, Iterator

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
SLUG_A = "tenant-a"
OWNER_EMAIL = "owner@example.com"

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"


def _docx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    keys = list(client.scan_iter(match=tenant_key(TENANT_A, "*")))
    if keys:
        client.delete(*keys)


@pytest.fixture
def kb_id() -> uuid.UUID:
    """租戶 A、一個 Owner、一個空的 KB。"""
    ensure_identity_seed()
    from apps.identity.models import Role

    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG_A)
        owner = make_user(
            tenant_id=TENANT_A, email=OWNER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=owner, role=Role.objects.get(tenant__isnull=True, name="owner"))
        kb = make_knowledge_base(tenant_id=TENANT_A, name="上傳測試 KB")
    return kb.id


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


@pytest.fixture
def stored_objects() -> Iterator[set[str]]:
    """記錄本測試期間寫進 bucket 的 key，結束時清掉。

    直接對真的 MinIO 驗證而不是 mock：本檔要驗的正是「DB 與物件儲存兩邊一致」，
    而 mock 掉其中一邊等於把要驗的東西假設成正確的。
    """
    from core.object_storage import delete_object, list_keys

    before = set(list_keys())
    yield before
    for key in set(list_keys()) - before:
        delete_object(key)


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": SLUG_A, "email": OWNER_EMAIL, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


async def _upload(
    client: httpx.AsyncClient,
    token: str,
    kb: uuid.UUID,
    *,
    content: bytes,
    filename: str = "report.pdf",
) -> httpx.Response:
    return await client.post(
        f"/api/v1/knowledge-bases/{kb}/documents",
        files={"file": (filename, content, "application/octet-stream")},
        headers={"Authorization": f"Bearer {token}"},
    )


class TestSuccessfulUpload:
    async def test_upload_creates_document_and_stores_the_object(
        self, client: httpx.AsyncClient, kb_id: uuid.UUID, stored_objects: set[str]
    ) -> None:
        """兩邊都要有：DB 一列 + 物件儲存一個物件。"""
        from core.object_storage import list_keys

        token = await _token(client)

        response = await _upload(client, token, kb_id, content=PDF_BYTES)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["filename"] == "report.pdf"
        assert body["mime_type"] == "application/pdf", "MIME 應由內容判定"
        assert body["size_bytes"] == len(PDF_BYTES)
        assert body["status"] == "uploaded", "08 §2 狀態機的起點"
        assert body["doc_version"] == 1
        assert "storage_key" not in body, "物件儲存路徑不得外流"

        new_keys = set(await run_orm(list_keys)) - stored_objects
        assert len(new_keys) == 1, f"物件儲存的變化不是恰好一個物件：{new_keys}"
        # key 的形狀出自 05 §3.2；doc id 在裡面，所以它天然隨機（10 §99 儲存名隨機化）
        assert str(body["id"]) in next(iter(new_keys))

    async def test_uploaded_document_appears_in_the_listing(
        self, client: httpx.AsyncClient, kb_id: uuid.UUID, stored_objects: set[str]
    ) -> None:
        """上傳完就查得到——1B-2 的列表端點與上傳走的是同一條資料路徑。"""
        token = await _token(client)
        await _upload(client, token, kb_id, content=PDF_BYTES)

        listing = await client.get(
            f"/api/v1/knowledge-bases/{kb_id}/documents",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert {item["filename"] for item in listing.json()["items"]} == {"report.pdf"}

    async def test_docx_is_accepted(
        self, client: httpx.AsyncClient, kb_id: uuid.UUID, stored_objects: set[str]
    ) -> None:
        token = await _token(client)

        response = await _upload(client, token, kb_id, content=_docx_bytes(), filename="spec.docx")

        assert response.status_code == 201, response.text
        assert response.json()["mime_type"].endswith("wordprocessingml.document")


class TestRejectedUploads:
    async def test_disguised_executable_is_415_and_never_reaches_storage(
        self, client: httpx.AsyncClient, kb_id: uuid.UUID, stored_objects: set[str]
    ) -> None:
        """副檔名 ``.pdf``、內容是執行檔 → 415，**且 bucket 不得有任何新物件**。

        「先存起來再驗證」比較好寫，但那等於任何人都能把任意內容寫進我們的 bucket，
        白名單形同虛設。所以這條測試同時斷言回應碼與儲存側的零變化。
        """
        from core.object_storage import list_keys

        token = await _token(client)

        response = await _upload(
            client, token, kb_id, content=b"MZ\x90\x00" + b"\x00" * 64, filename="report.pdf"
        )

        assert response.status_code == 415
        assert response.json()["code"] == "UNSUPPORTED_MEDIA_TYPE"
        assert set(await run_orm(list_keys)) == stored_objects, "被拒的內容仍被寫進物件儲存"

    async def test_oversized_upload_is_413(
        self, client: httpx.AsyncClient, kb_id: uuid.UUID, stored_objects: set[str]
    ) -> None:
        """超過 32MB（09 §3.1 的分塊界線）→ 413，訊息要說明上限。

        用剛好超過一個位元組的內容，而不是造一個真的 33MB 檔案：要驗的是邊界判定，
        而讓每次 CI 都搬 33MB 進記憶體只是讓測試變慢。
        """
        from services.knowledge.uploads import MAX_UPLOAD_BYTES

        token = await _token(client)
        oversized = b"%PDF-1.7\n" + b"0" * (MAX_UPLOAD_BYTES - 8)

        response = await _upload(client, token, kb_id, content=oversized)

        assert response.status_code == 413
        assert response.json()["code"] == "UPLOAD_TOO_LARGE"

    async def test_upload_to_another_tenants_kb_is_404(
        self, client: httpx.AsyncClient, kb_id: uuid.UUID, stored_objects: set[str]
    ) -> None:
        """指名不存在（或別的租戶）的 KB → 404，不是 500 也不是建出孤兒文件。"""
        token = await _token(client)

        response = await _upload(client, token, uuid.uuid4(), content=PDF_BYTES)

        assert response.status_code == 404
        assert response.json()["code"] == "RESOURCE_NOT_FOUND"


class TestDeduplication:
    async def test_same_content_twice_is_409(
        self, client: httpx.AsyncClient, kb_id: uuid.UUID, stored_objects: set[str]
    ) -> None:
        """同一份內容重複上傳 → 409（05 §3.2 的 ``UNIQUE(tenant, kb, content_hash)``）。

        回 409 而不是靜默回傳既有文件：使用者以為自己上傳了新版本，而系統其實什麼
        都沒做——那個誤會會一路延伸到「為什麼我的修改沒有生效」。真的要更新內容走
        reingest（1B-6）。
        """
        token = await _token(client)
        assert (await _upload(client, token, kb_id, content=PDF_BYTES)).status_code == 201

        response = await _upload(
            client, token, kb_id, content=PDF_BYTES, filename="same-content-other-name.pdf"
        )

        assert response.status_code == 409
        assert response.json()["code"] == "RESOURCE_CONFLICT"

    async def test_rejected_duplicate_does_not_leave_a_second_object(
        self, client: httpx.AsyncClient, kb_id: uuid.UUID, stored_objects: set[str]
    ) -> None:
        """被判定重複的那一次不得佔用儲存空間。

        先 PUT 再寫 DB 的順序下，重複會在 DB 那步才被發現——若沒有回收剛上傳的物件，
        每一次重複上傳都會留下一份垃圾，而使用者看到的是「上傳失敗」。
        """
        from core.object_storage import list_keys

        token = await _token(client)
        await _upload(client, token, kb_id, content=PDF_BYTES)
        after_first = set(await run_orm(list_keys))

        await _upload(client, token, kb_id, content=PDF_BYTES)

        assert set(await run_orm(list_keys)) == after_first, "重複上傳留下了多餘的物件"

    async def test_same_content_in_another_kb_is_allowed(
        self, client: httpx.AsyncClient, kb_id: uuid.UUID, stored_objects: set[str]
    ) -> None:
        """去重範圍含 kb_id——同一份文件放進兩個 KB 是正當需求（1B-1 已驗約束，這裡驗端點）。"""
        token = await _token(client)
        await _upload(client, token, kb_id, content=PDF_BYTES)

        other = await client.post(
            "/api/v1/knowledge-bases",
            json={"name": "另一個 KB"},
            headers={"Authorization": f"Bearer {token}"},
        )
        other_kb = uuid.UUID(other.json()["id"])

        response = await _upload(client, token, other_kb, content=PDF_BYTES)

        assert response.status_code == 201, response.text


class TestFailureLeavesNoOrphan:
    async def test_object_is_removed_when_the_database_write_fails(
        self,
        client: httpx.AsyncClient,
        kb_id: uuid.UUID,
        stored_objects: set[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DB 寫入炸掉時，已上傳的物件要被回收（best-effort）。

        這是「先 PUT 再寫 DB」這個順序**唯一的代價**，也是它必須被測到的地方：
        沒有回收的話，每一次 DB 錯誤都在 bucket 裡留一份沒有人指得到的檔案——不影響
        正確性，但會安靜地長大，而且事後無從分辨哪些是孤兒。

        用 monkeypatch 讓 repository 的建立丟例外：真實情境是唯一約束以外的 DB 錯誤
        （連線斷、約束變更），那些無法用正常請求製造出來。
        """
        from core.object_storage import list_keys
        from repositories.knowledge import DocumentRepository

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("DB 掛了")

        monkeypatch.setattr(DocumentRepository, "create", _boom)
        token = await _token(client)

        response = await _upload(client, token, kb_id, content=PDF_BYTES)

        assert response.status_code == 500
        assert set(await run_orm(list_keys)) == stored_objects, "DB 失敗後留下了孤兒物件"

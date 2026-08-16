"""驗收：`/conversations` 的行為（09 §1.1／§2.4、13 §3 工作包 1D-2）。

分工比照 knowledge 那組：本檔驗「進來之後回的東西對不對」，權限與擁有者判定在
`test_conversation_permissions.py`。

**這一包第一次引入 cursor 分頁**（09 §1.1）。前面的 knowledge 端點都是一次回全部，
那在 KB 與文件上勉強說得過去；訊息不行——它是無上限成長的，一場長對話不分頁就是
一次把幾百則全部吐出去。而**分頁是 API 契約**：之後才補會是 breaking change，前端
已經寫好的呼叫端全部要改。

四件事做錯了不會報錯：

1. **游標要能走完且不重不漏**。實作分頁最常見的錯是邊界用 `>=` 而不是 `>`（每頁重複
   一筆）或排序鍵不唯一（同一個 `created_at` 的兩筆會在翻頁時互相擠掉）。兩者都只在
   資料剛好跨頁時出現，而開發時的資料量通常剛好不會。
2. **訊息一律時間正序**。倒著給 LLM 會直接改變語意（1D-5 要用同一條路徑組 context），
   而前端倒著顯示只是「看起來怪」——後者有人會回報，前者沒有。
3. **游標是不透明的**。讓 client 自己拼 `?cursor=<id>` 的話，那個格式就變成契約的一
   部分，之後改排序鍵就會打破所有既有的呼叫端。
4. **回應不夾帶內部欄位**（同 1B-2 的規則）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from api.main import create_app
from common.passwords import hash_password
from core.redis import get_redis, tenant_key
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.conversation import make_conversation, make_message
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG_A = "tenant-a"
SLUG_B = "tenant-b"
OWNER_EMAIL = "owner@example.com"

# 09 §1.1：預設 20、上限 100。
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    for tenant_id in (TENANT_A, TENANT_B):
        keys = list(client.scan_iter(match=tenant_key(tenant_id, "*")))
        if keys:
            client.delete(*keys)


@pytest.fixture
def owner() -> uuid.UUID:
    ensure_identity_seed()
    from apps.identity.models import Role

    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG_A)
        user = make_user(
            tenant_id=TENANT_A, email=OWNER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=user, role=Role.objects.get(tenant__isnull=True, name="owner"))
    return uuid.UUID(str(user.id))


# ── 資料 fixture 一律同步 ──────────────────────────────────────
#
# Django ORM 是同步的，在 async 測試函式裡直接建資料會被 `SynchronousOnlyOperation`
# 擋下（同 test_knowledge_permissions.py 的 scenario、1C-4 的 seeded_kb）。因此每個
# 情境各給一個 fixture，測試只拿 id。


@pytest.fixture
def empty_conversation(owner: uuid.UUID) -> uuid.UUID:
    with tenant_scope(TENANT_A):
        return uuid.UUID(str(make_conversation(tenant_id=TENANT_A, user_id=owner).id))


@pytest.fixture
def two_conversations(owner: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """一場普通的、一場已釘選的——驗「部分更新不誤傷沒給的欄位」。"""
    with tenant_scope(TENANT_A):
        plain = make_conversation(tenant_id=TENANT_A, user_id=owner, title="原標題")
        pinned = make_conversation(tenant_id=TENANT_A, user_id=owner, pinned=True)
        return uuid.UUID(str(plain.id)), uuid.UUID(str(pinned.id))


@pytest.fixture
def silent_and_active(owner: uuid.UUID) -> None:
    """一場沒發言過、一場剛聊完——驗列表排序（NULL 要排後面）。"""
    with tenant_scope(TENANT_A):
        make_conversation(tenant_id=TENANT_A, user_id=owner, title="沒講過話")
        active = make_conversation(tenant_id=TENANT_A, user_id=owner, title="剛聊完")
        make_message(conversation=active, content="哈囉")


@pytest.fixture
def conversation_with_five(owner: uuid.UUID) -> uuid.UUID:
    with tenant_scope(TENANT_A):
        conversation = make_conversation(tenant_id=TENANT_A, user_id=owner)
        for index in range(5):
            make_message(conversation=conversation, content=f"第 {index} 則")
        return uuid.UUID(str(conversation.id))


@pytest.fixture
def conversation_with_many(owner: uuid.UUID) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """47 則——刻意**不是**頁大小的整數倍，最後一頁才會是半滿的。

    整數倍的話，「走完了卻還給游標」這個 bug 反而不會被觸發。
    """
    with tenant_scope(TENANT_A):
        conversation = make_conversation(tenant_id=TENANT_A, user_id=owner)
        ids = [
            uuid.UUID(str(make_message(conversation=conversation, content=f"第 {index} 則").id))
            for index in range(47)
        ]
        return uuid.UUID(str(conversation.id)), ids


@pytest.fixture
def conversation_with_one(owner: uuid.UUID) -> uuid.UUID:
    with tenant_scope(TENANT_A):
        conversation = make_conversation(tenant_id=TENANT_A, user_id=owner)
        make_message(conversation=conversation, content="只有一則")
        return uuid.UUID(str(conversation.id))


@pytest.fixture
def conversation_with_citations(owner: uuid.UUID) -> tuple[uuid.UUID, list[dict[str, object]]]:
    citations: list[dict[str, object]] = [
        {"chunk_id": str(uuid.uuid4()), "doc_id": str(uuid.uuid4()), "score": 0.9}
    ]
    with tenant_scope(TENANT_A):
        conversation = make_conversation(tenant_id=TENANT_A, user_id=owner)
        make_message(conversation=conversation, role="assistant", citations=citations)
        return uuid.UUID(str(conversation.id)), citations


@pytest.fixture
def other_tenant_conversation(owner: uuid.UUID) -> uuid.UUID:
    ensure_identity_seed()
    with tenant_scope(TENANT_B):
        make_tenant(id=TENANT_B, slug=SLUG_B)
        other_user = make_user(tenant_id=TENANT_B, email="b@example.com")
        return uuid.UUID(str(make_conversation(tenant_id=TENANT_B, user_id=other_user.id).id))


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


class TestCrud:
    async def test_create_then_read_back(self, client: httpx.AsyncClient, owner: uuid.UUID) -> None:
        token = await _token(client)
        kb_id = str(uuid.uuid4())

        created = await client.post(
            "/api/v1/conversations",
            json={"title": "第一場對話", "kb_ids": [kb_id]},
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        conversation_id = created.json()["id"]

        detail = await client.get(f"/api/v1/conversations/{conversation_id}", headers=_auth(token))

        assert detail.status_code == 200
        body = detail.json()
        assert body["title"] == "第一場對話"
        assert body["kb_ids"] == [kb_id]
        # 新對話是空的——前端不必為了顯示「0 則」再打一次。
        assert body["message_count"] == 0
        assert body["last_message_at"] is None

    async def test_patch_updates_only_given_fields(
        self, client: httpx.AsyncClient, two_conversations: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """部分更新只動有給的欄位。

        把沒給的欄位當成「設為空」寫回去的話，使用者改一次標題就會把釘選狀態清掉
        ——而那不會有錯誤訊息。
        """
        token = await _token(client)
        conversation_id, with_pin_id = two_conversations

        renamed = await client.patch(
            f"/api/v1/conversations/{conversation_id}",
            json={"title": "新標題"},
            headers=_auth(token),
        )
        pinned_only = await client.patch(
            f"/api/v1/conversations/{with_pin_id}",
            json={"status": "archived"},
            headers=_auth(token),
        )

        assert renamed.status_code == 200
        assert renamed.json()["title"] == "新標題"
        assert pinned_only.json()["status"] == "archived"
        assert pinned_only.json()["pinned"] is True, "沒給的欄位被清掉了"

    async def test_delete_is_soft(
        self, client: httpx.AsyncClient, empty_conversation: uuid.UUID
    ) -> None:
        """刪除是軟刪除（05 §5.4），而且刪掉之後列表與詳情都要看不到。

        只寫 `deleted_at` 卻沒從查詢排除的話，使用者會看到自己剛刪掉的對話還在。
        """
        token = await _token(client)

        deleted = await client.delete(
            f"/api/v1/conversations/{empty_conversation}", headers=_auth(token)
        )
        detail = await client.get(
            f"/api/v1/conversations/{empty_conversation}", headers=_auth(token)
        )
        listed = await client.get("/api/v1/conversations", headers=_auth(token))

        assert deleted.status_code == 204
        assert detail.status_code == 404
        assert listed.json()["items"] == []

    async def test_recently_active_conversations_come_first(
        self, client: httpx.AsyncClient, silent_and_active: None
    ) -> None:
        """列表依最後訊息時間排序（05 §4 的索引就是為這個建的）。

        剛建立、還沒發言的對話 `last_message_at` 是 NULL——預設的 NULLS FIRST 會讓
        空對話霸佔列表頂端，而那是使用者最不想看到的東西。
        """
        token = await _token(client)

        listed = await client.get("/api/v1/conversations", headers=_auth(token))

        titles = [item["title"] for item in listed.json()["items"]]
        assert titles == ["剛聊完", "沒講過話"], titles


class TestMessageReading:
    async def test_messages_are_returned_in_chronological_order(
        self, client: httpx.AsyncClient, conversation_with_five: uuid.UUID
    ) -> None:
        """**一律時間正序。**

        倒著給 LLM 會直接改變語意（1D-5 用同一條路徑組 context），而前端倒著顯示只是
        「看起來怪」——後者有人會回報，前者沒有。
        """
        token = await _token(client)

        response = await client.get(
            f"/api/v1/conversations/{conversation_with_five}/messages", headers=_auth(token)
        )

        contents = [item["content"] for item in response.json()["items"]]
        assert contents == [f"第 {index} 則" for index in range(5)]

    async def test_a_message_carries_its_citations(
        self,
        client: httpx.AsyncClient,
        conversation_with_citations: tuple[uuid.UUID, list[dict[str, object]]],
    ) -> None:
        """引用要原樣回得出來——1E 的引用面板靠它渲染。"""
        conversation_id, citations = conversation_with_citations
        token = await _token(client)

        response = await client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=_auth(token)
        )

        assert response.json()["items"][0]["citations"] == citations

    async def test_messages_of_an_unknown_conversation_are_404(
        self, client: httpx.AsyncClient, owner: uuid.UUID
    ) -> None:
        token = await _token(client)

        response = await client.get(
            f"/api/v1/conversations/{uuid.uuid4()}/messages", headers=_auth(token)
        )

        assert response.status_code == 404


class TestPagination:
    """09 §1.1 的 cursor 分頁。**這是 API 契約**，之後改是 breaking change。"""

    async def test_the_default_page_size_is_twenty(
        self, client: httpx.AsyncClient, conversation_with_many: tuple[uuid.UUID, list[uuid.UUID]]
    ) -> None:
        conversation_id, _ = conversation_with_many
        token = await _token(client)

        response = await client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=_auth(token)
        )

        body = response.json()
        assert len(body["items"]) == DEFAULT_LIMIT
        assert body["next_cursor"], "還有資料卻沒有給游標"

    async def test_walking_the_cursor_covers_everything_exactly_once(
        self, client: httpx.AsyncClient, conversation_with_many: tuple[uuid.UUID, list[uuid.UUID]]
    ) -> None:
        """**不重不漏。**

        分頁最常見的兩個錯：邊界用 `>=` 而不是 `>`（每頁重複一筆）、排序鍵不唯一
        （同一個 `created_at` 的兩筆在翻頁時互相擠掉）。兩者都只在資料剛好跨頁時
        出現，而開發時的資料量通常剛好不會。
        """
        conversation_id, message_ids = conversation_with_many
        total = len(message_ids)
        token = await _token(client)

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):  # 上限只是防呆，正常 3 頁走完
            url = f"/api/v1/conversations/{conversation_id}/messages?limit=20"
            if cursor:
                url += f"&cursor={cursor}"
            body = (await client.get(url, headers=_auth(token))).json()
            seen.extend(item["content"] for item in body["items"])
            cursor = body["next_cursor"]
            if not cursor:
                break

        assert cursor is None, "走完了卻還給游標"
        assert len(seen) == total, f"取到 {len(seen)} 筆，應為 {total}"
        assert len(set(seen)) == total, "有重複"
        assert seen == [f"第 {index} 則" for index in range(total)], "順序在翻頁時亂掉"

    async def test_the_last_page_has_no_cursor(
        self, client: httpx.AsyncClient, conversation_with_one: uuid.UUID
    ) -> None:
        """沒有下一頁時 `next_cursor` 是 null——client 用它決定要不要繼續。

        永遠回一個游標的話，前端會無限往下捲，而每一次都拿到空清單。
        """
        token = await _token(client)

        body = (
            await client.get(
                f"/api/v1/conversations/{conversation_with_one}/messages", headers=_auth(token)
            )
        ).json()

        assert len(body["items"]) == 1
        assert body["next_cursor"] is None

    @pytest.mark.parametrize("limit", [0, -1, MAX_LIMIT + 1])
    async def test_out_of_range_limit_is_rejected(
        self, client: httpx.AsyncClient, empty_conversation: uuid.UUID, limit: int
    ) -> None:
        """上限 100（09 §1.1）。

        沒有上限的話，一個 `limit=1000000` 會讓 DB 把整場對話撈出來——不會失敗，
        只是那幾秒對所有租戶都很慢。
        """
        token = await _token(client)

        response = await client.get(
            f"/api/v1/conversations/{empty_conversation}/messages?limit={limit}",
            headers=_auth(token),
        )

        assert response.status_code == 422, response.text

    async def test_a_malformed_cursor_is_rejected_not_ignored(
        self, client: httpx.AsyncClient, empty_conversation: uuid.UUID
    ) -> None:
        """壞掉的游標要回 422，**不是安靜地當成第一頁**。

        當成第一頁的話，前端的無限捲動會永遠停在開頭而看起來像「載不完」；回 500
        則是把一個 client 的錯誤記成我們的錯誤。
        """
        token = await _token(client)

        response = await client.get(
            f"/api/v1/conversations/{empty_conversation}/messages?cursor=not-a-real-cursor",
            headers=_auth(token),
        )

        assert response.status_code == 422, response.text

    async def test_the_cursor_is_opaque(
        self, client: httpx.AsyncClient, conversation_with_many: tuple[uuid.UUID, list[uuid.UUID]]
    ) -> None:
        """游標不得是可猜的 id 或位移。

        看起來像 id 的話，client 遲早會自己拼一個，而那個格式就變成契約的一部分——
        之後改排序鍵會打破所有既有的呼叫端。
        """
        conversation_id, message_ids = conversation_with_many
        token = await _token(client)

        body = (
            await client.get(
                f"/api/v1/conversations/{conversation_id}/messages", headers=_auth(token)
            )
        ).json()

        cursor = body["next_cursor"]
        assert cursor
        assert str(message_ids[DEFAULT_LIMIT - 1]) not in cursor, "游標直接暴露了 id"


class TestResponseShape:
    async def test_internal_fields_are_not_leaked(
        self, client: httpx.AsyncClient, conversation_with_one: uuid.UUID
    ) -> None:
        """`tenant_id` 與 `deleted_at` 不該出去（鐵則 4 與 1B-2 的同一條規則）。"""
        token = await _token(client)

        detail = await client.get(
            f"/api/v1/conversations/{conversation_with_one}", headers=_auth(token)
        )
        messages = await client.get(
            f"/api/v1/conversations/{conversation_with_one}/messages", headers=_auth(token)
        )

        for response in (detail, messages):
            assert "tenant_id" not in response.text
            assert "deleted_at" not in response.text

    def test_operation_ids_are_stable(self) -> None:
        """`operation_id` 決定前端 codegen 的函式名（鐵則 10）。"""
        schema = create_app().openapi()
        paths = schema["paths"]

        assert paths["/api/v1/conversations"]["get"]["operationId"] == "conversations_list"
        assert paths["/api/v1/conversations"]["post"]["operationId"] == "conversations_create"
        assert (
            paths["/api/v1/conversations/{conversation_id}/messages"]["get"]["operationId"]
            == "conversations_list_messages"
        )


class TestTenantIsolation:
    async def test_another_tenants_conversation_is_404(
        self, client: httpx.AsyncClient, other_tenant_conversation: uuid.UUID
    ) -> None:
        """跨租戶回 404 而不是 403（09 §2.3 的資源類規則）。"""
        token = await _token(client)
        response = await client.get(
            f"/api/v1/conversations/{other_tenant_conversation}", headers=_auth(token)
        )

        assert response.status_code == 404

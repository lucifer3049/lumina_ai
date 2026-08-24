"""驗收：`core/object_storage.py` —— 全 repo 唯一的物件儲存入口。

與 `test_infra_object_storage.py` 的分工：那一檔驗**基礎設施本身**（bucket 存在、
版本化開啟、匿名讀取被拒、應用憑證不是 root）；本檔驗**我們寫的那層 client** 的行為。

四件事非驗不可：

1. **key 一律帶租戶前綴**（鐵則 4 在物件儲存這一側的實施點）。前綴若留給呼叫端自己
   記得加，遲早有人漏掉——而漏掉的後果是兩個租戶的檔案混在同一個 prefix 底下，
   日後任何「以 prefix 掃描」的清理或匯出都會跨租戶。
2. **前綴是真的被擋，不是靠約定**（`TestPrefixEnforcement`）。只有 `build_document_key`
   會加前綴，而它是「請呼叫端自願使用」的函式；讀寫三兄弟若接受任意 key，任何一個
   自己組 key 的呼叫端、或一個被寫壞的 ``documents.storage_key``，就能讀寫別的租戶的
   物件——無例外、無 log。物件 key 會持久化在 DB 裡，這一點比 Redis 那側更要緊。
3. **timeout 一定要設**（11 §4.1：MinIO 30s）。沒有 timeout 時 MinIO 卡住會慢慢佔滿
   threadpool，症狀是「整個網站變慢」而不是「物件儲存有問題」。
4. **刪除是冪等的**：清理流程會重跑，刪一個已經不存在的 key 不該炸。
5. **測試資料的 key 形狀要跟著正式路徑走**（`TestFactoryKeysMatchProduction`）。
   `DocumentFactory` 自己組 `storage_key`，而它一度停在 05 §3.2 的舊形狀
   （`tenant-{slug}`）——於是任何拿 factory 文件去走物件儲存的測試都會撞
   `CrossTenantObjectKeyError`，而錯誤看起來像被測程式碼有 bug。
"""

from __future__ import annotations

import uuid

import pytest

from core.exceptions import CrossTenantObjectKeyError, TenantContextMissingError
from core.object_storage import build_document_key, delete_object, get_object, put_object
from core.tenant import tenant_context
from tests.conftest import TENANT_A, TENANT_B

CONTENT = b"%PDF-1.7\nhello\n"


class TestKeyNaming:
    def test_key_carries_the_tenant_and_kb(self) -> None:
        """key 形狀 = ``tenant-{tenant_id}/kb/{kb_id}/{doc_id}``（05 §3.2）。

        05 §3.2 寫的是 ``tenant-{slug}``，這裡用 **tenant_id**：slug 是可變的
        （租戶改名），而物件 key 一旦寫進 DB 的 ``storage_key`` 就不能再變——用 slug
        的話，改名之後所有既有物件的 key 都對不上，而症狀是「舊文件突然打不開」。
        """
        kb_id, doc_id = uuid.uuid4(), uuid.uuid4()

        with tenant_context(TENANT_A):
            key = build_document_key(kb_id=kb_id, document_id=doc_id)

        assert key == f"tenant-{TENANT_A}/kb/{kb_id}/{doc_id}"

    def test_two_tenants_never_share_a_prefix(self) -> None:
        kb_id, doc_id = uuid.uuid4(), uuid.uuid4()

        with tenant_context(TENANT_A):
            key_a = build_document_key(kb_id=kb_id, document_id=doc_id)
        with tenant_context(TENANT_B):
            key_b = build_document_key(kb_id=kb_id, document_id=doc_id)

        assert key_a != key_b
        assert not key_a.startswith(key_b.split("/")[0])

    def test_missing_tenant_context_raises(self) -> None:
        """缺 TenantContext 一律 raise（鐵則 4 的 Fail Fast），不得回一個沒有前綴的 key。

        回無前綴 key 的話，檔案會落在 bucket 根目錄——不屬於任何租戶，而且不會有錯誤。
        """
        with pytest.raises(TenantContextMissingError):
            build_document_key(kb_id=uuid.uuid4(), document_id=uuid.uuid4())


class TestRoundTrip:
    """讀寫都在 `tenant_context` 內——這一層的每個操作都要有租戶可比（見下一個 class）。"""

    @pytest.fixture
    def key(self) -> str:
        with tenant_context(TENANT_A):
            return build_document_key(kb_id=uuid.uuid4(), document_id=uuid.uuid4())

    def test_put_then_get_returns_the_same_bytes(self, key: str) -> None:
        with tenant_context(TENANT_A):
            try:
                put_object(key, CONTENT, content_type="application/pdf")
                assert get_object(key) == CONTENT
            finally:
                delete_object(key)

    def test_delete_is_idempotent(self, key: str) -> None:
        """刪一個不存在的 key 不該炸——清理流程會重跑（08 §6 的冪等原則）。"""
        with tenant_context(TENANT_A):
            put_object(key, CONTENT, content_type="application/pdf")
            delete_object(key)
            delete_object(key)  # 第二次不得 raise

    def test_get_missing_key_raises_a_domain_error(self, key: str) -> None:
        """讀不存在的物件要是我們自己的例外，不是 botocore 的 ClientError。

        botocore 的例外會一路冒到 api/main.py 的兜底 handler 變成 500，而且訊息裡
        帶著 bucket 名稱與端點——那是內部拓撲（鐵則 9）。
        """
        from core.exceptions import ObjectNotFoundError

        with tenant_context(TENANT_A), pytest.raises(ObjectNotFoundError):
            get_object(key)


class TestPrefixEnforcement:
    """讀寫三兄弟自己擋跨租戶的 key，不倚賴呼叫端有沒有用 `build_document_key`。

    這一組測試的存在理由是「防線不能建立在自願上」：模組 docstring 宣稱租戶前綴強制
    在這一層，而在這些測試之前，那句話只是口號——`put_object` 之類接受任意字串。
    """

    @pytest.fixture
    def foreign_key(self) -> str:
        """另一個租戶的合法 key——正是「storage_key 被寫壞」時會拿到的東西。"""
        with tenant_context(TENANT_B):
            return build_document_key(kb_id=uuid.uuid4(), document_id=uuid.uuid4())

    def test_put_rejects_another_tenants_key(self, foreign_key: str) -> None:
        with tenant_context(TENANT_A), pytest.raises(CrossTenantObjectKeyError):
            put_object(foreign_key, CONTENT, content_type="application/pdf")

    def test_get_rejects_another_tenants_key(self, foreign_key: str) -> None:
        with tenant_context(TENANT_A), pytest.raises(CrossTenantObjectKeyError):
            get_object(foreign_key)

    def test_delete_rejects_another_tenants_key(self, foreign_key: str) -> None:
        """刪除**不是**冪等的藉口：刪掉別人的物件與刪掉不存在的物件差別無限大。"""
        with tenant_context(TENANT_A), pytest.raises(CrossTenantObjectKeyError):
            delete_object(foreign_key)

    def test_a_key_without_any_prefix_is_rejected(self) -> None:
        """自己組的 key（漏了前綴）會落在 bucket 根目錄——不屬於任何租戶。"""
        with tenant_context(TENANT_A), pytest.raises(CrossTenantObjectKeyError):
            put_object("orphan.pdf", CONTENT, content_type="application/pdf")

    def test_prefix_match_is_not_merely_startswith_on_the_uuid(self) -> None:
        """前綴比對必須含尾端的 ``/``。

        少了它，``tenant-{A}extra/...`` 會通過 ``tenant-{A}`` 的比對。UUID 沒有規定
        誰不能是誰的前綴，而這種洞不會有症狀——直到有人真的撞上為止。
        """
        with tenant_context(TENANT_A), pytest.raises(CrossTenantObjectKeyError):
            get_object(f"tenant-{TENANT_A}evil/kb/x/y")

    def test_no_tenant_context_is_a_hard_stop(self) -> None:
        """缺 context 時「比不了就放行」是最糟的選項（Fail Fast，鐵則 4）。"""
        with tenant_context(TENANT_A):
            key = build_document_key(kb_id=uuid.uuid4(), document_id=uuid.uuid4())

        with pytest.raises(TenantContextMissingError):
            get_object(key)

    def test_list_keys_is_the_documented_exemption(self) -> None:
        """`list_keys` 刻意不擋：維運與測試要看得見不屬於當前租戶的 key。

        釘住這個豁免是為了讓它保持是「有意的例外」而不是「漏掉的那一個」——把它也
        擋掉的話，`tests/api/test_document_upload.py` 的孤兒物件檢查就無從寫起。
        """
        from core.object_storage import list_keys

        with tenant_context(TENANT_B):
            foreign = build_document_key(kb_id=uuid.uuid4(), document_id=uuid.uuid4())
            put_object(foreign, CONTENT, content_type="application/pdf")
        try:
            with tenant_context(TENANT_A):
                assert foreign in list_keys(f"tenant-{TENANT_B}/")
        finally:
            with tenant_context(TENANT_B):
                delete_object(foreign)


def test_client_has_timeouts_configured() -> None:
    """11 §4.1：所有對外呼叫必有 timeout（MinIO 30s）。

    直接讀 client 的設定而不是量一次真的逾時：後者要讓 MinIO 真的卡住，而那在 CI 上
    做不到；設定值本身就是這條規則的落地點。
    """
    from config.settings.app_settings import get_app_settings
    from core.object_storage import get_s3_client

    settings = get_app_settings()
    config = get_s3_client().meta.config

    assert config.connect_timeout == settings.s3_timeout_seconds
    assert config.read_timeout == settings.s3_timeout_seconds


@pytest.mark.django_db(transaction=True)
class TestFactoryKeysMatchProduction:
    """`DocumentFactory` 的 `storage_key` 必須與 `build_document_key` 逐字相同。

    factory 自己組 key（它建的是 DB 列，不經過上傳路徑），所以兩邊是**兩份各自
    寫死的字串**——而 1B-3 把正式路徑從 05 §3.2 的 `tenant-{slug}` 改成 tenant_id 時，
    factory 沒有跟著改，那個偏差活到了 2B。

    **偏差不會讓 factory 出錯，會讓別人出錯**：讀寫三兄弟每次都比對租戶前綴，
    對不上就 `CrossTenantObjectKeyError`——而它冒出來的位置在被測的服務裡，看起來
    像產品 bug。這條測試把兩份字串釘在一起，讓下一次改形狀時 factory 立刻紅。
    """

    def test_the_factory_key_is_exactly_what_production_would_build(self) -> None:
        from tests.factories.identity import make_tenant, tenant_scope
        from tests.factories.knowledge import make_document, make_knowledge_base

        with tenant_scope(TENANT_A):
            make_tenant(id=TENANT_A, slug="tenant-a")
            kb = make_knowledge_base(tenant_id=TENANT_A)
            document = make_document(kb=kb)
            expected = build_document_key(
                kb_id=uuid.UUID(str(kb.id)), document_id=uuid.UUID(str(document.id))
            )

        assert document.storage_key == expected

    def test_a_factory_document_survives_the_prefix_guard(self) -> None:
        """把它真的送進讀寫三兄弟——形狀對不對，這一關說了算。"""
        from tests.factories.identity import make_tenant, tenant_scope
        from tests.factories.knowledge import make_document, make_knowledge_base

        with tenant_scope(TENANT_A):
            make_tenant(id=TENANT_A, slug="tenant-a")
            document = make_document(kb=make_knowledge_base(tenant_id=TENANT_A))

        with tenant_context(TENANT_A):
            key = str(document.storage_key)
            put_object(key, CONTENT, content_type="application/pdf")
            try:
                assert get_object(key) == CONTENT
            finally:
                delete_object(key)

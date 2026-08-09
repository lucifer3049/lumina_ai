"""驗收：`core/object_storage.py` —— 全 repo 唯一的物件儲存入口。

與 `test_infra_object_storage.py` 的分工：那一檔驗**基礎設施本身**（bucket 存在、
版本化開啟、匿名讀取被拒、應用憑證不是 root）；本檔驗**我們寫的那層 client** 的行為。

三件事非驗不可：

1. **key 一律帶租戶前綴**（鐵則 4 在物件儲存這一側的實施點）。前綴若留給呼叫端自己
   記得加，遲早有人漏掉——而漏掉的後果是兩個租戶的檔案混在同一個 prefix 底下，
   日後任何「以 prefix 掃描」的清理或匯出都會跨租戶。
2. **timeout 一定要設**（11 §4.1：MinIO 30s）。沒有 timeout 時 MinIO 卡住會慢慢佔滿
   threadpool，症狀是「整個網站變慢」而不是「物件儲存有問題」。
3. **刪除是冪等的**：清理流程會重跑，刪一個已經不存在的 key 不該炸。
"""

from __future__ import annotations

import uuid

import pytest

from core.exceptions import TenantContextMissingError
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
    @pytest.fixture
    def key(self) -> str:
        with tenant_context(TENANT_A):
            return build_document_key(kb_id=uuid.uuid4(), document_id=uuid.uuid4())

    def test_put_then_get_returns_the_same_bytes(self, key: str) -> None:
        try:
            put_object(key, CONTENT, content_type="application/pdf")
            assert get_object(key) == CONTENT
        finally:
            delete_object(key)

    def test_delete_is_idempotent(self, key: str) -> None:
        """刪一個不存在的 key 不該炸——清理流程會重跑（08 §6 的冪等原則）。"""
        put_object(key, CONTENT, content_type="application/pdf")
        delete_object(key)
        delete_object(key)  # 第二次不得 raise

    def test_get_missing_key_raises_a_domain_error(self, key: str) -> None:
        """讀不存在的物件要是我們自己的例外，不是 botocore 的 ClientError。

        botocore 的例外會一路冒到 api/main.py 的兜底 handler 變成 500，而且訊息裡
        帶著 bucket 名稱與端點——那是內部拓撲（鐵則 9）。
        """
        from core.exceptions import ObjectNotFoundError

        with pytest.raises(ObjectNotFoundError):
            get_object(key)


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

"""驗收：per-tenant DEK 與憑證落地（10 §5、05 §3.3 的 `credential_ref`，工作包 2C-2）。

`test_crypto.py` 驗的是「加密本身對不對」，這一檔驗的是 envelope 的另外兩半：
**每個租戶一把 DEK**（用 KEK 包起來存），以及**密文入 DB、明文只在使用當下出現**。

envelope 的意義就在那個中間層：KEK 換掉時只要重包 N 把 DEK，不必把每一筆憑證都重新
加密一遍。少了它（明文直接用 KEK 加密）程式一樣會動，而代價在**輪替金鑰的那一天**才
出現——那時要重寫每一列。

四件錯了都不會有例外的事：

1. **兩個租戶共用一把 DEK**。隔離退化成「同一把鑰匙開所有的門」，而 RLS 之外沒有任何
   東西擋得住一次寫錯 tenant 條件的查詢。
2. **明文落地**。欄位叫 `ciphertext` 不代表裡面是密文——寫錯一次就永久留在 DB 與備份
   裡，而讀得回來、跑得起來，沒有任何症狀。
3. **DEK 每次呼叫重新產生**。上一輪存的東西全部解不開，而錯誤訊息指向 provider。
4. **解密結果被記進 log**（10 §5：「解密僅在使用當下、不落 log」）。
"""

from __future__ import annotations

import uuid

import pytest

from apps.platform.models import Credential, TenantDataKey
from core.exceptions import NotFoundError
from services.platform.credentials import CredentialService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)

SECRET = "sk-live-0123456789abcdef"


@pytest.fixture
def tenants() -> None:
    for tenant_id, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=slug)


def _rows(tenant_id: uuid.UUID) -> list[Credential]:
    with tenant_scope(tenant_id):
        return list(Credential.objects.all())


class TestRoundTrip:
    def test_a_stored_secret_comes_back(self, tenants: None) -> None:
        service = CredentialService()
        service.put(TENANT_A, "openai_api_key", SECRET)

        assert service.get_secret(TENANT_A, "openai_api_key") == SECRET

    def test_writing_twice_replaces_it(self, tenants: None) -> None:
        """換金鑰是常見操作（provider 輪替）。新增一列的話，`get_secret` 得決定「哪
        一列才算數」，而那個決定遲早會挑到舊的——症狀是「換了 key 還是 401」。"""
        service = CredentialService()
        service.put(TENANT_A, "openai_api_key", SECRET)

        service.put(TENANT_A, "openai_api_key", "sk-live-new")

        assert service.get_secret(TENANT_A, "openai_api_key") == "sk-live-new"
        assert len(_rows(TENANT_A)) == 1

    def test_an_unknown_name_is_not_found(self, tenants: None) -> None:
        """回 `None` 的話，呼叫端會把 `None` 當成 key 送給 provider，而錯誤訊息是
        provider 回的 401——與「金鑰過期」長得一模一樣。"""
        with pytest.raises(NotFoundError):
            CredentialService().get_secret(TENANT_A, "openai_api_key")


class TestAtRest:
    def test_the_plaintext_is_not_in_the_row(self, tenants: None) -> None:
        """**本檔第 2 條。** 寫錯一次就永久留在 DB 與備份裡，而一切照常運作。"""
        CredentialService().put(TENANT_A, "openai_api_key", SECRET)

        row = _rows(TENANT_A)[0]
        assert SECRET.encode() not in bytes(row.ciphertext)
        assert SECRET not in repr(row)

    def test_the_row_keeps_a_hint_but_not_the_secret(self, tenants: None) -> None:
        """畫面要能讓人認出「這是哪一把」（末四碼），而那不足以還原金鑰。

        存前四碼的話，`sk-live` 這種帶前綴的 key 等於洩漏了種類與環境（live/test）。
        """
        CredentialService().put(TENANT_A, "openai_api_key", SECRET)

        row = _rows(TENANT_A)[0]
        assert row.hint == SECRET[-4:]
        assert len(row.hint) <= 4


class TestPerTenantDataKeys:
    def test_each_tenant_gets_its_own_dek(self, tenants: None) -> None:
        service = CredentialService()
        service.put(TENANT_A, "openai_api_key", SECRET)
        service.put(TENANT_B, "openai_api_key", SECRET)

        with tenant_scope(TENANT_A):
            key_a = TenantDataKey.objects.get()
        with tenant_scope(TENANT_B):
            key_b = TenantDataKey.objects.get()
        assert bytes(key_a.wrapped_key) != bytes(key_b.wrapped_key), "兩個租戶共用了一把 DEK"

    def test_the_dek_is_stable_across_calls(self, tenants: None) -> None:
        """**本檔第 3 條**：每次呼叫重產一把的話，上一輪存的東西全部解不開。"""
        service = CredentialService()
        service.put(TENANT_A, "a", SECRET)
        service.put(TENANT_A, "b", "second")

        with tenant_scope(TENANT_A):
            assert TenantDataKey.objects.count() == 1
        assert service.get_secret(TENANT_A, "a") == SECRET

    def test_the_dek_itself_is_wrapped_not_plain(self, tenants: None) -> None:
        """DEK 明文落地的話，這一層就只是「把金鑰換個地方放」。"""
        from core.crypto import KEY_BYTES, load_kek

        CredentialService().put(TENANT_A, "openai_api_key", SECRET)

        with tenant_scope(TENANT_A):
            wrapped = bytes(TenantDataKey.objects.get().wrapped_key)
        assert len(wrapped) > KEY_BYTES, "看起來像未加密的裸金鑰"
        assert wrapped != load_kek()

    def test_one_tenants_secret_cannot_be_read_by_another(self, tenants: None) -> None:
        """**本檔第 1 條。** RLS 是第一道，DEK 是第二道——兩道都要在。"""
        service = CredentialService()
        service.put(TENANT_A, "openai_api_key", SECRET)

        with pytest.raises(NotFoundError):
            service.get_secret(TENANT_B, "openai_api_key")


class TestDescribeIsWriteOnly:
    """09 §2.6 的那一列：「唯寫不回讀明文」。"""

    def test_describe_never_returns_the_secret(self, tenants: None) -> None:
        service = CredentialService()
        service.put(TENANT_A, "openai_api_key", SECRET)

        described = service.describe(TENANT_A)

        assert [item.name for item in described] == ["openai_api_key"]
        assert SECRET not in repr(described)
        assert described[0].hint == SECRET[-4:]

    def test_describe_is_empty_for_a_tenant_without_credentials(self, tenants: None) -> None:
        assert CredentialService().describe(TENANT_B) == []


class TestDelete:
    def test_a_deleted_credential_is_gone(self, tenants: None) -> None:
        """**硬刪而不是軟刪**：軟刪的密文留在 DB 裡，而它的用途只剩「還原一把使用者
        認為已經撤銷的金鑰」——那與撤銷的語意相反。"""
        service = CredentialService()
        service.put(TENANT_A, "openai_api_key", SECRET)

        service.delete(TENANT_A, "openai_api_key")

        assert _rows(TENANT_A) == []
        with pytest.raises(NotFoundError):
            service.get_secret(TENANT_A, "openai_api_key")

    def test_deleting_something_that_is_not_there_is_not_found(self, tenants: None) -> None:
        with pytest.raises(NotFoundError):
            CredentialService().delete(TENANT_A, "openai_api_key")

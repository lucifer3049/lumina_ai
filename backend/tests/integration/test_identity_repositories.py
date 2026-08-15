"""驗收：Identity 的 Repository（第一道防線，鐵則 4）。

與 `test_rls_identity.py` 的分工要講清楚，否則兩邊看起來在驗同一件事：

- 那一檔繞過 Repository 下原生 SQL，驗的是**資料庫**擋不擋得住。
- 本檔走 Repository，驗的是**程式**有沒有把租戶條件帶上，以及缺租戶 context 時
  是否 Fail Fast。

每個查詢都包在 `unit_of_work()` 裡，這不是形式：`tenant_context` 設的是 Python
端的 contextvar（Repository 讀它組 filter），而 RLS 讀的是 PostgreSQL 的交易區域
參數 ``app.tenant_id``——後者由 UoW 在交易開始時設定。只設前者的話 DB 那頭沒有
租戶，policy 會把本租戶的列也一起濾掉，測試看到的是空集合。正式路徑上 Service
方法本來就是交易邊界（11 §4.3），所以測試照同樣形狀寫才有代表性。

兩道都要。只有 RLS 的話，漏帶 filter 的查詢會安靜地回空集合或少一半資料，
症狀是「功能怪怪的」而不是錯誤；只有 filter 的話，漏寫一次就外洩。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from core.exceptions import TenantContextMissingError
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import RoleRepository, UserRepository
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import (
    make_system_role,
    make_tenant,
    make_tenant_role,
    make_user,
    tenant_scope,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def users_in_both_tenants() -> Iterator[dict[str, uuid.UUID]]:
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a")
        user_a = make_user(tenant_id=TENANT_A, email="same@example.com")

    with tenant_scope(TENANT_B):
        make_tenant(id=TENANT_B, slug="tenant-b")
        user_b = make_user(tenant_id=TENANT_B, email="same@example.com")

    yield {"a": user_a.id, "b": user_b.id}


def test_repository_lists_only_the_current_tenant(
    users_in_both_tenants: dict[str, uuid.UUID],
) -> None:
    with tenant_context(TENANT_A), unit_of_work():
        ids = {user.id for user in UserRepository().get_queryset()}

    assert ids == {users_in_both_tenants["a"]}


def test_same_email_in_two_tenants_resolves_to_different_users(
    users_in_both_tenants: dict[str, uuid.UUID],
) -> None:
    """同一個 email 在兩個租戶各有一個帳號，查出來必須是各自那一個。

    `UNIQUE(tenant_id, email)` 是**租戶內**唯一——不同公司用同一個信箱是正常的
    （顧問、外包、同一個人在兩家客戶各有帳號）。若哪天有人把它改成全域唯一，
    第二家客戶的建立流程會以「這個 email 已被使用」失敗，而那則訊息完全不會
    提到是別的租戶佔用了它。
    """
    with tenant_context(TENANT_A), unit_of_work():
        found_a = UserRepository().get_by_email("same@example.com")
    with tenant_context(TENANT_B), unit_of_work():
        found_b = UserRepository().get_by_email("same@example.com")

    assert found_a is not None and found_a.id == users_in_both_tenants["a"]
    assert found_b is not None and found_b.id == users_in_both_tenants["b"]


def test_repository_refuses_to_run_without_tenant_context(
    users_in_both_tenants: dict[str, uuid.UUID],
) -> None:
    """缺租戶 context 一律 raise，不是回空集合（鐵則 4 / Fail Fast）。

    回空集合的話，「忘了設租戶」與「這個租戶真的沒有資料」長得一模一樣，
    而前者是 bug、後者是正常狀態。
    """
    assert users_in_both_tenants

    with pytest.raises(TenantContextMissingError):
        list(UserRepository().get_queryset())


def test_role_repository_includes_system_roles(
    users_in_both_tenants: dict[str, uuid.UUID],
) -> None:
    """角色查詢必須同時涵蓋「系統內建角色」與「本租戶自訂角色」。

    這條會逼出一個設計問題：`TenantScopedRepository.get_queryset()` 的過濾是
    ``tenant_id = 當前租戶``，而系統角色的 ``tenant_id`` 是 NULL——用基底的預設
    行為查角色，**四個系統角色一個都不會回來**。

    也就是說 RoleRepository 必須顯式覆寫查詢起點（``tenant_id IS NULL OR
    tenant_id = 當前租戶``），且那個覆寫要與 `identity_role` 的 RLS policy 條件
    一致。兩邊不一致的症狀分兩種：程式比 policy 寬 → 查詢回空（DB 擋掉）；
    程式比 policy 窄 → 使用者看不到自己的角色。兩種都不會有錯誤訊息。
    """
    assert users_in_both_tenants
    # 名字**刻意不用** SYSTEM_ROLE_PERMISSIONS 裡的四個（owner/admin/editor/viewer）：
    # 那四個由 migration 種進去，`uq_role_tenant_name` 是 (tenant_id, name) 的唯一
    # 約束，於是這行在種子還在的資料庫上會直接 IntegrityError。序列跑之所以看不到，
    # 是因為排在前面的 transactional 測試已經把種子 TRUNCATE 掉了——也就是這條測試
    # 原本依賴「別人先破壞資料」才會過，而那個順序在平行下不成立。
    # 斷言要的只是「一個 tenant_id IS NULL 的角色查得到」，用哪個名字無關。
    system_role = make_system_role(name="system-role-visibility-probe")

    with tenant_scope(TENANT_A):
        custom_role = make_tenant_role(tenant_id=TENANT_A, name="tenant-a-only")

    with tenant_context(TENANT_A), unit_of_work():
        visible = {role.id for role in RoleRepository().get_queryset()}

    assert system_role.id in visible, "系統角色沒被查出來——RoleRepository 未處理 tenant_id IS NULL"
    assert custom_role.id in visible


def test_role_repository_excludes_other_tenants_custom_roles() -> None:
    """放寬 NULL 的同時不能把別人的自訂角色一起放進來。"""
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a")
        role_a = make_tenant_role(tenant_id=TENANT_A, name="tenant-a-only")

    with tenant_scope(TENANT_B):
        make_tenant(id=TENANT_B, slug="tenant-b")

    with tenant_context(TENANT_B), unit_of_work():
        visible = {role.id for role in RoleRepository().get_queryset()}

    assert role_a.id not in visible

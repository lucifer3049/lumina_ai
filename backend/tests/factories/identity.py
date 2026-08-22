"""Identity 的 factory（apps/identity/models.py）。

**建立租戶資料時必須先有租戶 context**：`tenants` 的 RLS policy 是
``id = current_setting('app.tenant_id')``，其餘表是 ``tenant_id = ...``。也就是說
連「建立租戶」這件事本身都要在該租戶的 context 內做——先產 uuid、把它設進
context、再 INSERT。這不是繞過隔離，而是隔離的自然結果：系統裡沒有「站在所有
租戶之外」的位置（我們刻意不建 BYPASSRLS 角色，見 core/uow.py）。

**為什麼對外露出的是 `make_*` 函式而不是 factory 類別**：factory_boy 的
metaclass 讓 mypy 看不出 ``UserFactory(...)`` 會回傳什麼，在 strict 之下每個
呼叫點都會報「呼叫未標型別的函式」。把 factory 類別關在本模組內、對外只給有
回傳型別的薄包裝，代價是多幾行，換到的是**測試檔本身維持完整的型別檢查**
——放寬 `tests.*` 的 strict 設定會讓所有測試都少一層保護，那個範圍太大了。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import factory

from apps.identity.models import Permission, Role, RolePermission, Tenant, User, UserRole
from core.tenant import tenant_context
from core.uow import unit_of_work


@contextmanager
def tenant_scope(tenant_id: uuid.UUID) -> Iterator[uuid.UUID]:
    """在指定租戶的 context **與交易**內建立資料。

    兩層都要：`tenant_context` 設的是 Python 端的 contextvar（Repository 讀它），
    而 RLS 的 ``WITH CHECK`` 讀的是 PostgreSQL 的交易區域參數 ``app.tenant_id``
    ——後者由 `unit_of_work` 在交易開始時設定。只進 context 不開交易的話，
    INSERT 會因為 policy 拿不到租戶而被拒，錯誤訊息是
    ``new row violates row-level security policy``，看不出缺的是交易。
    """
    with tenant_context(tenant_id) as tid, unit_of_work():
        yield tid


class TenantFactory(factory.django.DjangoModelFactory[Tenant]):
    class Meta:
        model = Tenant

    id = factory.LazyFunction(uuid.uuid4)
    name = factory.Sequence(lambda n: f"Tenant {n}")
    slug = factory.Sequence(lambda n: f"tenant-{n}")
    plan = "free"
    status = "active"
    settings: dict[str, object] = {}


class UserFactory(factory.django.DjangoModelFactory[User]):
    class Meta:
        model = User

    id = factory.LazyFunction(uuid.uuid4)
    tenant = factory.SubFactory(TenantFactory)
    # email 帶序號而非固定值：UNIQUE(tenant_id, email) 的測試要能建出「同租戶內
    # 重複」與「跨租戶相同」兩種資料，固定值會讓前者在無關的測試裡誤觸發。
    email = factory.Sequence(lambda n: f"user{n}@example.com")
    # 真實雜湊由 1A-3 的 AuthService 產生；這裡只要一個明顯不是明文密碼的值，
    # 避免有人日後把它當成「可以登入的密碼」使用。
    password_hash = "argon2id$placeholder-not-a-real-hash"
    display_name = factory.Sequence(lambda n: f"User {n}")
    status = "active"


class SystemRoleFactory(factory.django.DjangoModelFactory[Role]):
    """系統內建角色：``tenant`` 為 None、``is_system`` 為 True，全租戶共用。"""

    class Meta:
        model = Role

    id = factory.LazyFunction(uuid.uuid4)
    tenant = None
    name = "viewer"
    is_system = True


class TenantRoleFactory(factory.django.DjangoModelFactory[Role]):
    """租戶自訂角色（1A 不開放建立，但 RLS 要能區分它與系統角色）。"""

    class Meta:
        model = Role

    id = factory.LazyFunction(uuid.uuid4)
    tenant = factory.SubFactory(TenantFactory)
    name = factory.Sequence(lambda n: f"custom-role-{n}")
    is_system = False


class PermissionFactory(factory.django.DjangoModelFactory[Permission]):
    """全域字典表，沒有 tenant_id、不開 RLS（理由見 test_rls_identity.py）。

    **走 ``admin`` 連線**：應用角色對這張表只剩 SELECT（0012_platform_table_grants
    ——正當的寫入者只有 migration）。用它的測試因此要在 `django_db` 標記上列出
    ``databases=["default", "admin"]``，同 `tests/seed.py` 的補種。
    """

    class Meta:
        model = Permission
        database = "admin"

    id = factory.LazyFunction(uuid.uuid4)
    code = factory.Sequence(lambda n: f"resource{n}:read")
    description = ""


class UserRoleFactory(factory.django.DjangoModelFactory[UserRole]):
    class Meta:
        model = UserRole

    user = factory.SubFactory(UserFactory)
    role = factory.SubFactory(SystemRoleFactory)
    tenant = factory.SelfAttribute("user.tenant")


class RolePermissionFactory(factory.django.DjangoModelFactory[RolePermission]):
    class Meta:
        model = RolePermission

    role = factory.SubFactory(SystemRoleFactory)
    permission = factory.SubFactory(PermissionFactory)
    # 跟著 role 走：系統角色的授權列 tenant 為 None（全租戶可見），
    # 租戶自訂角色的授權列則綁該租戶。
    tenant = factory.SelfAttribute("role.tenant")


# ── 對外介面：有回傳型別的薄包裝 ────────────────────────────────


def _with_tenant(kwargs: dict[str, Any]) -> dict[str, Any]:
    """把 ``tenant_id=<uuid>`` 換成 ``tenant=<Tenant 實例>``。

    測試寫 ``make_user(tenant_id=TENANT_A)`` 比較直覺，但 factory 的 `SubFactory`
    看到 ``tenant`` 沒被指定就會**再造一個租戶**，然後 model 同時收到 tenant 與
    tenant_id 兩個值。這裡先解析成實例，SubFactory 就不會被觸發。
    """
    tenant_id = kwargs.pop("tenant_id", None)
    if tenant_id is not None:
        kwargs["tenant"] = Tenant.objects.get(id=tenant_id)
    return kwargs


def make_tenant(**kwargs: Any) -> Tenant:
    return cast(Tenant, TenantFactory(**kwargs))


def make_user(**kwargs: Any) -> User:
    return cast(User, UserFactory(**_with_tenant(kwargs)))


def make_system_role(**kwargs: Any) -> Role:
    return cast(Role, SystemRoleFactory(**kwargs))


def make_tenant_role(**kwargs: Any) -> Role:
    return cast(Role, TenantRoleFactory(**_with_tenant(kwargs)))


def make_permission(**kwargs: Any) -> Permission:
    return cast(Permission, PermissionFactory(**kwargs))


def make_user_role(**kwargs: Any) -> UserRole:
    return cast(UserRole, UserRoleFactory(**kwargs))


def make_role_permission(**kwargs: Any) -> RolePermission:
    return cast(RolePermission, RolePermissionFactory(**kwargs))

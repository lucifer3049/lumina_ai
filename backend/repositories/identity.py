"""Identity 的 Repository —— 租戶隔離的第一道防線（鐵則 4）。

第二道是 DB 的 RLS policy（apps/identity/migrations/0002_rls.py）。兩道的條件
**必須一致**，不一致時的症狀分兩種，而且都沒有錯誤訊息：

- 程式比 policy 寬：查詢送到 DB 被 policy 濾掉 → 回空集合。
- 程式比 policy 窄：DB 放行但程式先濾掉了 → 使用者看不到本來該看到的資料。

因此 :class:`RoleRepository` 的查詢條件與 `identity_role` 的 policy 條件是逐字
對應的（``tenant_id IS NULL OR tenant_id = 當前租戶``）；改一邊就要改另一邊。
"""

from __future__ import annotations

import uuid

from django.db.models import F, Q, QuerySet
from django.utils import timezone

from apps.identity.models import Role, Tenant, TenantDirectory, User, UserRole
from core.tenant import get_current_tenant_id
from repositories.base import TenantScopedRepository


class UserRepository(TenantScopedRepository[User]):
    model = User

    def get_by_email(self, email: str) -> User | None:
        """租戶內以 email 找人。

        走 :meth:`get_queryset` 而非 ``User.objects``——email 在本專案只保證
        **租戶內**唯一（``UNIQUE(tenant_id, email)``），全域查會在「同一個人在
        兩家客戶各有帳號」時撈到別的租戶那一筆。
        """
        return self.get_queryset().filter(email=email).first()

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self.get_queryset().filter(id=user_id).first()

    def bump_token_version(self, user_id: uuid.UUID) -> None:
        """把 ``token_version`` +1，讓該使用者手上所有 token 失效（10 §2.1）。

        用 ``F()`` 而非讀出來加一再存回去：後者在兩個並行請求下會互相覆蓋，
        結果是只加了一次——而「改密碼之後舊 token 還能用」正是這個欄位要防的事。
        """
        self.get_queryset().filter(id=user_id).update(token_version=F("token_version") + 1)

    def touch_last_login(self, user_id: uuid.UUID) -> None:
        self.get_queryset().filter(id=user_id).update(last_login_at=timezone.now())

    def upgrade_password_hash(self, user_id: uuid.UUID, encoded_hash: str) -> None:
        """雜湊參數調強之後的就地升級（只有登入當下手上有明文密碼）。"""
        self.get_queryset().filter(id=user_id).update(password_hash=encoded_hash)

    def set_password_hash(self, user_id: uuid.UUID, encoded_hash: str) -> None:
        self.get_queryset().filter(id=user_id).update(password_hash=encoded_hash)

    def set_display_name(self, user_id: uuid.UUID, display_name: str) -> None:
        self.get_queryset().filter(id=user_id).update(display_name=display_name)

    def set_status(self, user_id: uuid.UUID, status: str) -> None:
        self.get_queryset().filter(id=user_id).update(status=status)

    def create(
        self,
        *,
        email: str,
        display_name: str,
        password_hash: str,
        tenant_id: uuid.UUID | None = None,
    ) -> User:
        """建立使用者。

        ``tenant_id`` 平常不必給——它由 TenantContext 決定（鐵則 4）。只有租戶
        開通那一刻例外：那時 context 裡的租戶就是正在建立的這一個，顯式帶上讓
        意圖清楚，也避免有人以為那裡漏了什麼。
        """
        return User.objects.create(
            tenant_id=tenant_id or get_current_tenant_id(operation="UserRepository.create"),
            email=email,
            display_name=display_name,
            password_hash=password_hash,
        )


class TenantRepository(TenantScopedRepository[Tenant]):
    """租戶自己也是 tenant-scoped 的——只是比對欄位是 ``id`` 而不是 ``tenant_id``。

    因此本類別覆寫查詢起點：基底的 ``filter(tenant_id=...)`` 在這張表上根本沒有
    那個欄位。條件與 `identity_tenant` 的 RLS policy 逐字對應（1A-2）。
    """

    model = Tenant

    def get_queryset(self) -> QuerySet[Tenant]:
        tenant_id = get_current_tenant_id(operation="TenantRepository.get_queryset")
        return self.model._default_manager.filter(id=tenant_id)

    def create(self, *, tenant_id: uuid.UUID, name: str, slug: str) -> Tenant:
        return Tenant.objects.create(id=tenant_id, name=name, slug=slug)

    def current(self) -> Tenant | None:
        return self.get_queryset().first()

    def rename(self, name: str) -> None:
        self.get_queryset().update(name=name)


class TenantDirectoryRepository:
    """slug → tenant_id 的查詢。**唯一一個不繼承 TenantScopedRepository 的 repository。**

    這是刻意的例外，而且只有一個理由：登入發生在租戶身分存在**之前**，
    無法套用 tenant filter（見 apps/identity/models.py 的 `TenantDirectory`）。

    因此本類別的能力被限制到最小——只能用 slug 換 id，回傳的東西不含任何客戶
    資料。任何「用它繞過租戶過濾去讀別的表」的需求都應該被當成設計錯誤處理。
    """

    def get_active_tenant_id(self, slug: str) -> uuid.UUID | None:
        row = (
            TenantDirectory.objects.filter(slug=slug, status="active")
            .values_list("tenant_id", flat=True)
            .first()
        )
        return row


class RoleRepository(TenantScopedRepository[Role]):
    """角色查詢：本租戶自訂角色 ＋ 全租戶共用的系統內建角色。

    **必須覆寫基底**：`TenantScopedRepository.get_queryset` 的條件是
    ``tenant_id = 當前租戶``，而系統角色的 ``tenant_id`` 是 NULL——用基底的預設
    行為查角色，Owner/Admin/Editor/Viewer 一個都不會回來，而權限判定會安靜地
    退化成「這個使用者沒有任何角色」。
    """

    model = Role

    def get_queryset(self) -> QuerySet[Role]:
        tenant_id = get_current_tenant_id(operation="RoleRepository.get_queryset")
        return self.model._default_manager.filter(Q(tenant__isnull=True) | Q(tenant_id=tenant_id))

    def assign(self, *, user_id: uuid.UUID, role_name: str, tenant_id: uuid.UUID) -> None:
        """指派角色。指到的是**系統角色那一份**（``tenant_id`` 為 NULL）。

        不複製一份到租戶底下：系統角色的保證就是「所有租戶共用同一份、不可修改」，
        複本會讓權限判定時而拿到這份、時而拿到那份。
        """
        role = self.get_queryset().get(name=role_name, tenant__isnull=True)
        UserRole.objects.get_or_create(user_id=user_id, role_id=role.id, tenant_id=tenant_id)

    def names_for_user(self, user_id: uuid.UUID) -> tuple[str, ...]:
        """這個使用者實際被指派的角色名稱，供 JWT 的 ``roles`` claim 使用。

        走 `get_queryset()` 出發（而不是 `Role.objects`）確保系統角色與本租戶角色
        的範圍與 RLS policy 一致；再以 `user_roles` 收斂到「這個人有的」。
        """
        return tuple(
            self.get_queryset().filter(user_roles__user_id=user_id).values_list("name", flat=True)
        )

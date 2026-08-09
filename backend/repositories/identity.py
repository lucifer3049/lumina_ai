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

from apps.identity.models import Role, TenantDirectory, User
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

    def names_for_user(self, user_id: uuid.UUID) -> tuple[str, ...]:
        """這個使用者實際被指派的角色名稱，供 JWT 的 ``roles`` claim 使用。

        走 `get_queryset()` 出發（而不是 `Role.objects`）確保系統角色與本租戶角色
        的範圍與 RLS policy 一致；再以 `user_roles` 收斂到「這個人有的」。
        """
        return tuple(
            self.get_queryset().filter(user_roles__user_id=user_id).values_list("name", flat=True)
        )

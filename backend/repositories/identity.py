"""Identity 的 Repository —— 租戶隔離的第一道防線（鐵則 4）。

第二道是 DB 的 RLS policy（apps/identity/migrations/0002_rls.py）。兩道的條件
**必須一致**，不一致時的症狀分兩種，而且都沒有錯誤訊息：

- 程式比 policy 寬：查詢送到 DB 被 policy 濾掉 → 回空集合。
- 程式比 policy 窄：DB 放行但程式先濾掉了 → 使用者看不到本來該看到的資料。

因此 :class:`RoleRepository` 的查詢條件與 `identity_role` 的 policy 條件是逐字
對應的（``tenant_id IS NULL OR tenant_id = 當前租戶``）；改一邊就要改另一邊。
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.identity.models import Role, User
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

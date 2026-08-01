"""Repository 基底 —— 租戶隔離的唯一實施點（ADR-002）。

鐵則 4：所有 Repository 繼承 :class:`TenantScopedRepository`，tenant filter
由基底自動注入，子類別不得自行組 queryset 起點。TenantContext 缺失一律
raise（Fail Fast），不提供「預設租戶」或「無租戶模式」的退路。

**PgBouncer transaction mode 的限制**（05 §5.5）：本層禁用 session 級功能
——advisory lock、``SET``（``SET LOCAL`` 在交易內安全）、server-side prepared
statement（已於 settings 以 ``prepare_threshold=None`` 關閉）。
"""

from __future__ import annotations

from django.db.models import Model, QuerySet

from core.tenant import get_current_tenant_id


class TenantScopedRepository[M: Model]:
    """所有 tenant-scoped 資料存取的起點。"""

    model: type[M]

    def get_queryset(self) -> QuerySet[M]:
        """已套用 tenant filter 的 queryset。

        子類別的每一個查詢都必須由此出發——直接用 ``self.model.objects``
        會繞過租戶隔離。
        """
        tenant_id = get_current_tenant_id(
            operation=f"{type(self).__name__}.get_queryset",
        )
        return self.model._default_manager.filter(tenant_id=tenant_id)

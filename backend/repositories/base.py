"""Repository 基底 —— 租戶隔離的唯一實施點（ADR-002）。

鐵則 4：所有 Repository 繼承 :class:`TenantScopedRepository`，tenant filter
由基底自動注入，子類別不得自行組 queryset 起點。TenantContext 缺失一律
raise（Fail Fast），不提供「預設租戶」或「無租戶模式」的退路。

**PgBouncer transaction mode 的限制**（05 §5.5）：本層禁用 session 級功能
——advisory lock、``SET``（``SET LOCAL`` 在交易內安全）、server-side prepared
statement（已於 settings 以 ``prepare_threshold=None`` 關閉）、以及 **server-side
cursor**（``QuerySet.iterator()``；已於 settings 以
``DISABLE_SERVER_SIDE_CURSORS=True`` 關閉，理由見該處）。
"""

from __future__ import annotations

import uuid

from django.db.models import Model, QuerySet
from django.utils import timezone

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


# **1D-1 從 `repositories/knowledge.py` 搬過來。** 軟刪除是 05 §5.4 的跨 context 規則
# （KB、document、conversation、prompt 都適用），放在某一個 context 的檔案裡，第二個
# 需要它的 context 就得 import 那個 context——而 ADR-006 的 bounded context 之間不該
# 互相依賴。這裡是共用基礎設施，不是任何一個 context。


class SoftDeletableRepository[M: Model](TenantScopedRepository[M]):
    """軟刪除的實體：預設查詢排除已刪除的列。

    覆寫 ``get_queryset`` 而不是要求每個查詢自己加條件——後者只要有一處漏掉，
    使用者就會看到已刪除的資料，而那一處不會有任何症狀。要撈已刪除的列（清理
    worker、還原功能）走 :meth:`including_deleted`，讓那個意圖在呼叫端顯式可見。
    """

    def get_queryset(self) -> QuerySet[M]:
        return super().get_queryset().filter(deleted_at__isnull=True)

    def including_deleted(self) -> QuerySet[M]:
        """含已刪除的列——只給清理 worker 與還原流程用。"""
        return super().get_queryset()

    def soft_delete(self, entity_id: uuid.UUID) -> int:
        """標記刪除；回傳影響的列數（0 = 不存在或已刪除）。

        不硬刪（05 §5.4）：硬刪要級聯 embeddings → chunks → documents，那是清理
        worker 分批做的事，在請求路徑上做會鎖表。
        """
        return self.get_queryset().filter(id=entity_id).update(deleted_at=timezone.now())

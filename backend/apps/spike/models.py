"""Spike 用最小 tenant-scoped 表。

ADR-001 鐵則 6：model 只有欄位、Meta、``__str__``；業務規則在 Service、
查詢在 Repository。

**索引與排序必須一致**——這是上一輪 spike 記下的教訓：
索引宣告為 ``(tenant_id, -created_at)``，但 repository 預設用 ``-id`` 排序，
執行計畫就會退化成 PK 反向掃描 + filter，索引完全用不到。spike 只有 2 個租戶
（目標資料佔 50%）所以看不出問題，500 個租戶時每個只佔 0.2%，要掃約 10,000 列
才湊得到 20 筆。因此 ``SpikeItemRepository`` 一律 ``order_by("-created_at")``。
"""

from __future__ import annotations

from django.db import models


class SpikeItem(models.Model):
    """壓測目標資料表；欄位刻意最小化，避免序列化成本汙染量測。"""

    tenant_id = models.UUIDField()
    title = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "spike_item"
        indexes = [
            # 欄位順序對應 repository 的 WHERE tenant_id = ? ORDER BY created_at DESC
            models.Index(
                fields=["tenant_id", "-created_at"],
                name="ix_spike_item_tenant_ct",
            ),
        ]

    def __str__(self) -> str:
        return f"SpikeItem({self.pk}, {self.title})"

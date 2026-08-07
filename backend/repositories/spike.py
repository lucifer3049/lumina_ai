"""Spike repository —— 壓測打的那條查詢。

本檔的方法全部是**同步**的：它們只會被 :func:`core.db.run_orm` 從 threadpool
呼叫，不得在 async 路徑直接 await（ADR-001）。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import TypedDict, cast

from apps.spike.models import SpikeItem
from repositories.base import TenantScopedRepository


class SpikeItemRow(TypedDict):
    """``latest()`` 的單列型別。

    刻意用 TypedDict 而非 Pydantic model：``values()`` 回的就是 dict，這裡只是
    把欄位契約標出來，**不產生任何 runtime 轉換**——壓測要量的是橋接開銷。
    轉成對外 DTO 是 api 層的事（``api/schemas/spike.py``）；repository 不得
    import api 層（鐵則 2）。
    """

    id: int
    title: str
    created_at: datetime


class SpikeItemRepository(TenantScopedRepository[SpikeItem]):
    model = SpikeItem

    def latest(self, limit: int) -> list[SpikeItemRow]:
        """取當前租戶最新 N 筆。

        ``order_by("-created_at")`` 必須與 ``ix_spike_item_tenant_ct``
        ``(tenant_id, -created_at)`` 一致——換成 ``-id`` 會讓執行計畫退化成
        PK 反向掃描 + filter，索引白宣告（見 apps/spike/models.py docstring）。

        用 ``values()`` 而非 ``browse`` 整個 model 實例：壓測要量的是橋接開銷，
        不是 Django model 實例化的成本。
        """
        queryset = (
            self.get_queryset().order_by("-created_at").values("id", "title", "created_at")[:limit]
        )
        # django-stubs 對 values() 只推得出泛型的列型別，收斂成本檔宣告的
        # SpikeItemRow。cast 不產生 runtime 成本（不逐列複製），壓測熱路徑上這點很重要。
        return list(cast("Iterable[SpikeItemRow]", queryset))

    def count_all(self) -> int:
        """當前租戶總筆數。

        唯一呼叫端是 ``tests/integration/test_tenant_scope.py``——隔離斷言要能說
        「租戶 A 只看得到自己那 3 筆」，筆數是最直接的表達。原 docstring 寫「供
        /healthz 確認 seed 資料到位」，而 ``/spike/healthz`` 從來沒有 count 過任何
        東西；那句話讓對應的 service 方法被當成活的而留了一條死路徑（已於 Phase 0
        結案審查移除）。
        """
        return self.get_queryset().count()

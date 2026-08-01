"""Spike service —— 唯一允許呼叫 Repository 的層（鐵則 2）。

這層在 spike 裡幾乎沒有業務邏輯，存在的意義是**把 async/sync 的邊界固定在
這裡**：endpoint 永遠 await service，service 永遠經 ``run_orm`` 進 ORM。
Phase 0 之後業務規則長出來時，位置已經預留好了。
"""

from __future__ import annotations

from core.db import run_orm
from repositories.spike import SpikeItemRepository, SpikeItemRow


class SpikeService:
    def __init__(self, repo: SpikeItemRepository | None = None) -> None:
        # 預設值方便 spike 直接用；DI 入口保留給測試替換 fake repo。
        self._repo = repo or SpikeItemRepository()

    async def latest_items(self, limit: int) -> list[SpikeItemRow]:
        return await run_orm(self._repo.latest, limit)

    async def count_items(self) -> int:
        return await run_orm(self._repo.count_all)

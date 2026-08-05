"""Spike service —— 唯一允許呼叫 Repository 的層（鐵則 2）。

這層在 spike 裡幾乎沒有業務邏輯，存在的意義是**把 async/sync 的邊界固定在
這裡**：endpoint 永遠 await service，service 永遠經 ``run_orm`` 進 ORM。
Phase 0 之後業務規則長出來時，位置已經預留好了。

**交易邊界也在這一層**（11 §4.3）：每個公開方法把自己的資料存取包進
:func:`core.uow.unit_of_work`。UoW 必須在 ``run_orm`` 送進 threadpool **之後**
才開——Django ORM 是同步的，在 event loop 上開交易會阻塞整個 loop（ADR-001）。
因此每個公開方法都配一個 ``_*_sync`` 私有方法，交易在後者裡面。

唯讀查詢為什麼也要包交易：RLS policy 對 ``SELECT`` 同樣生效（05 §5.1），而
policy 讀的 ``app.tenant_id`` 是交易區域參數——沒有交易就沒有那個參數，Phase 1
的 RLS 上線後這條查詢會回空集合或全表，兩者都不會有錯誤訊息。代價是每次查詢多
一次 ``BEGIN`` 與一次 ``set_config``；這筆成本會反映在 B 組壓測，基準線因此需要
重新量測（11 §1.4）。
"""

from __future__ import annotations

from core.db import run_orm
from core.uow import unit_of_work
from repositories.spike import SpikeItemRepository, SpikeItemRow


class SpikeService:
    def __init__(self, repo: SpikeItemRepository | None = None) -> None:
        # 預設值方便 spike 直接用；DI 入口保留給測試替換 fake repo。
        self._repo = repo or SpikeItemRepository()

    async def latest_items(self, limit: int) -> list[SpikeItemRow]:
        return await run_orm(self._latest_items_sync, limit)

    async def count_items(self) -> int:
        return await run_orm(self._count_items_sync)

    # ── 以下在 threadpool 執行緒上執行；交易邊界在這裡，不在 async 那側 ──

    def _latest_items_sync(self, limit: int) -> list[SpikeItemRow]:
        with unit_of_work():
            return self._repo.latest(limit)

    def _count_items_sync(self) -> int:
        with unit_of_work():
            return self._repo.count_all()

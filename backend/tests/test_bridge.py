"""A 組正確性測試 —— ADR-001 橋接的三個關鍵行為。

這三條就是本 spike 的 DoD。壓測數字只在這些測試全過的前提下才有意義——
一個會洩漏連線或串租戶的實作，跑得再快也沒有用。
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from django.db import close_old_connections

from core.db import run_orm
from core.exceptions import TenantContextMissingError
from core.tenant import get_current_tenant_id, tenant_context
from repositories.spike import SpikeItemRepository
from tests.conftest import TENANT_A


class TestOrmRunsOffEventLoop:
    """驗證 ORM 真的離開了 event loop 執行緒。"""

    async def test_runs_on_dedicated_orm_thread(self) -> None:
        loop_thread = threading.current_thread()

        def _which_thread() -> threading.Thread:
            return threading.current_thread()

        orm_thread = await run_orm(_which_thread)

        assert orm_thread is not loop_thread, "ORM 跑在 event loop 上，等於沒有橋接"
        assert orm_thread.name.startswith("orm"), (
            f"ORM 應跑在專用 executor 上，實際執行緒名稱：{orm_thread.name}"
        )

    async def test_concurrent_calls_use_multiple_threads(self) -> None:
        """並行呼叫應散到多條執行緒——否則 threadpool 等於沒生效。"""

        def _slow_ident() -> int:
            import time

            time.sleep(0.05)
            return threading.get_ident()

        idents = await asyncio.gather(*[run_orm(_slow_ident) for _ in range(4)])
        assert len(set(idents)) > 1, "4 個並行 ORM 呼叫全跑在同一條執行緒上"


class TestConnectionCleanupThread:
    """發現 2 的回歸測試：連線回收必須發生在跑 ORM 的那條執行緒上。

    這正是原設計（middleware 呼叫 close_old_connections）失效的原因——
    middleware 在 event loop 執行緒，而 Django connection 是 thread-local。
    """

    async def test_close_called_on_same_thread_as_orm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: dict[str, int] = {}

        def _spy() -> None:
            recorded["close_thread"] = threading.get_ident()
            close_old_connections()

        # 以字串路徑 patch：core.db 只是轉引 django.db 的名字，
        # 直接讀 core_db.close_old_connections 會踩到 mypy 的 no_implicit_reexport。
        monkeypatch.setattr("core.db.close_old_connections", _spy)

        def _orm_work() -> int:
            recorded["orm_thread"] = threading.get_ident()
            return 1

        await run_orm(_orm_work)

        assert "close_thread" in recorded, "close_old_connections 根本沒被呼叫"
        assert recorded["close_thread"] == recorded["orm_thread"], (
            "連線回收跑在別條執行緒上——threadpool 上的連線不會被釋放"
        )
        assert recorded["close_thread"] != threading.get_ident(), (
            "回收跑在 event loop 執行緒上，等於回到原本失效的設計"
        )


class TestTenantContextCrossesThread:
    """contextvars 必須能跨進 threadpool，否則租戶隔離在橋接處斷掉。"""

    async def test_tenant_id_visible_inside_threadpool(self) -> None:
        with tenant_context(TENANT_A):
            seen = await run_orm(get_current_tenant_id)
        assert seen == TENANT_A

    async def test_missing_tenant_raises_inside_threadpool(self) -> None:
        """缺 TenantContext 時必須 raise，不得默默回傳全部資料。"""
        repo = SpikeItemRepository()
        with pytest.raises(TenantContextMissingError):
            await run_orm(repo.latest, 10)

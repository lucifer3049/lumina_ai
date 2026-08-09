"""A 組正確性測試 —— ADR-001 橋接的三個關鍵行為。

這三條是 ADR-001 穿刺驗證的 DoD，橋接還在用就得一直綠。壓測數字只在這些測試
全過的前提下才有意義——一個會洩漏連線或串租戶的實作，跑得再快也沒有用。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from django.db import close_old_connections, connection

from core.db import run_orm
from core.exceptions import TenantContextMissingError
from core.tenant import get_current_tenant_id, tenant_context
from repositories.identity import UserRepository
from tests.conftest import TENANT_A


def _select_one() -> int:
    """真的打一次 DB。

    沒有實際查詢就不會開出連線——那樣「連線數沒漲」這件事會恆真，測了等於沒測。
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def _count_backends() -> int:
    """本測試資料庫上的連線數（含這條查詢自己用掉的那條）。

    基準值與事後值都用同一個函式量，自身那條連線在兩邊互相抵銷。
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


# 等連線數回落的上限。不開放成參數：只有一個呼叫端，而 ruff ASYNC109 會（正確地）
# 質疑 async 函式上的 timeout 參數——這裡要的是輪詢後回傳實際值好寫進錯誤訊息，
# 不是 asyncio.timeout 那種逾時即中斷的語意。
_SETTLE_TIMEOUT_SECONDS = 2.0


async def _count_backends_when_settled(baseline: int) -> int:
    """等連線數落回基準值，最多等 :data:`_SETTLE_TIMEOUT_SECONDS` 秒。

    ``pg_stat_activity`` 的列在 backend 真正結束時才移除，斷開與移除之間有極短
    的空窗。不等就直接斷言會偶發紅燈——而偶發紅燈比沒有測試更糟，大家會開始
    無視它。真的洩漏時連線不會自己消失，這裡只會多花 2 秒才失敗。
    """
    deadline = time.monotonic() + _SETTLE_TIMEOUT_SECONDS
    count = await run_orm(_count_backends)
    while count > baseline and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        count = await run_orm(_count_backends)
    return count


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
        """缺 TenantContext 時必須 raise，不得默默回傳全部資料。

        走真的 Repository（而不是自製的假物件）：要驗的是 contextvar 沒有跨進
        threadpool 時，**實際會被呼叫的那條程式碼**確實 fail fast。
        """
        repo = UserRepository()
        with pytest.raises(TenantContextMissingError):
            await run_orm(repo.get_by_email, "nobody@example.com")


@pytest.mark.slow
class TestConnectionsDoNotLeak:
    """發現 2 的第二條回歸測試 —— 結果面。

    ``TestConnectionCleanupThread`` 驗的是「清理函式有在對的執行緒被呼叫」，
    那是**行為面**：它不會因為 executor 換掉、``CONN_MAX_AGE`` 改動或連線在
    別處被重新開啟而失敗。這條補的是結果：大量呼叫之後，DB 上的連線數必須回到
    基準值。

    測試設定的 ``CONN_MAX_AGE`` 固定為 0（config/settings/test.py），所以每次
    呼叫後連線都該關掉。拿掉 ``_call_with_cleanup`` 裡的 ``close_old_connections()``
    ——也就是回到原本失效的設計——threadpool 執行緒上的連線會留著不放，這條紅。
    """

    async def test_connection_count_returns_to_baseline(self, transactional_db: object) -> None:
        baseline = await run_orm(_count_backends)

        # 先讓多條 threadpool 執行緒各開過一次連線。只跑單執行緒的話，
        # 「thread-local 連線沒回收」最多只漏一條，容易被雜訊蓋過去。
        await asyncio.gather(*[run_orm(_select_one) for _ in range(8)])
        for _ in range(200):
            await run_orm(_select_one)

        after = await _count_backends_when_settled(baseline)

        assert after <= baseline, (
            f"連線數從 {baseline} 漲到 {after}——threadpool 執行緒上的連線沒有被回收"
        )


class TestBridgeOverhead:
    """量測前提：橋接本身不得在熱路徑上做多餘的建構。"""

    async def test_wrapper_is_not_rebuilt_per_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``sync_to_async()`` 不得在每次 ``run_orm`` 呼叫時重建。

        重建一次不貴，但它落在壓測正在量的那條路徑上：量出來的數字會含一筆本來
        不該存在的成本，而且偏多少無從得知。11 §1.4 記的教訓是「環境雜訊會吃掉
        真實差異」，這裡是同一類問題的另一面——受測物本身混進了不屬於它的成本。

        包裝器應在 import 期建立一次；因此本測試期間 ``sync_to_async`` 的呼叫次數
        必須是 0。搬回函式內就會紅。
        """
        calls = 0

        def _spy(*args: object, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            raise AssertionError("不該在此處建立包裝器")

        monkeypatch.setattr("core.db.sync_to_async", _spy)

        def _noop() -> int:
            return 1

        for _ in range(3):
            await run_orm(_noop)

        assert calls == 0, f"run_orm 在呼叫時重建了 {calls} 次 sync_to_async 包裝器"

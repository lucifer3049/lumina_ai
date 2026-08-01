"""ORM 橋接：async → sync threadpool（ADR-001）。

**全 repo 唯一允許呼叫 Django ORM 的通道。** async endpoint 直接碰 ORM 會
阻塞 event loop；所有 ORM 存取都必須包成同步函式，交給 :func:`run_orm`。

連線回收的位置是本檔的重點（ADR-001 修訂紀錄 2026-08-01）：

    Django 的 DB connection 存在 **thread-local**，``close_old_connections()``
    只關得掉「呼叫它的那條執行緒」上的連線。原設計把它放在 FastAPI middleware
    ——那跑在 event loop 執行緒上，關的是一條不存在的連線，而 threadpool worker
    上真正開出來的連線沒有人回收，會持續累積。

    因此它必須在 :func:`_call_with_cleanup` 的 ``finally`` 裡呼叫：那裡與 ORM
    是同一條執行緒。

``close_old_connections()`` 的實際行為由 ``CONN_MAX_AGE`` 決定：
``0`` = 每次都關（等於每次 ORM 呼叫重建連線）；``300`` = 未逾時就留著重用。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import close_old_connections

# 顯式建立 executor，不用 asyncio 預設的那個。
# 理由：預設 executor 大小是 min(32, cpu+4) 且與其他 run_in_executor 使用者共用，
# 大小無法控制也無法觀測——而 threadpool 大小是 11 §1.3 要量的旋鈕之一。
_orm_executor = ThreadPoolExecutor(
    max_workers=settings.ORM_THREADPOOL_SIZE,
    thread_name_prefix="orm",
)


def _call_with_cleanup[R](fn: Callable[..., R], *args: Any, **kwargs: Any) -> R:
    """在 threadpool 執行緒上跑 ORM，並在同一條執行緒回收連線。"""
    try:
        return fn(*args, **kwargs)
    finally:
        # 必須在此處——見模組 docstring。搬到 middleware 會導致連線洩漏。
        close_old_connections()


# **在 import 期建立一次，不要搬進 run_orm。**
# 每次呼叫重建一個包裝器不貴，但它落在 B 組壓測正在量的那條路徑上——量出來的
# 數字會含一筆不屬於受測對象的成本，而且偏多少無從得知（locustfile.py 開頭的
# 教訓：環境雜訊已經會吃掉真實差異，受測物本身不該再自己加料）。
# 由 tests/test_bridge.py::TestBridgeOverhead 釘住。
_run_with_cleanup = sync_to_async(
    _call_with_cleanup,
    thread_sensitive=False,
    executor=_orm_executor,
)


async def run_orm[**P, R](fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """把同步的 ORM 函式送進受控 threadpool 執行。

    ``thread_sensitive=False``：不綁 asgiref 的主執行緒，才能真正並行。
    TenantContext 走 contextvars，``sync_to_async`` 會把當前 context 複製進
    threadpool 執行緒——租戶隔離因此得以跨執行緒保持（見 core/tenant.py）。
    複製發生在**每次呼叫**時，與包裝器本身建立幾次無關。
    """
    return cast("R", await _run_with_cleanup(fn, *args, **kwargs))


def orm_threadpool_size() -> int:
    """供 /healthz 與壓測報告回報實際生效的 threadpool 大小。"""
    return int(settings.ORM_THREADPOOL_SIZE)

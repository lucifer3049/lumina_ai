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
# 每次呼叫重建一個包裝器不貴，但它落在壓測正在量的那條路徑上——量出來的數字會含
# 一筆不屬於受測對象的成本，而且偏多少無從得知（11 §1.4 的教訓：環境雜訊已經會吃掉
# 真實差異，受測物本身不該再自己加料）。
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


def orm_runtime_knobs() -> dict[str, Any]:
    """橋接旋鈕的診斷回報（11 §4.2 的 ``/healthz`` 會用；spike 期的
    ``/spike/healthz`` 已於 1A-5 刪除，但守門留在這一層，見 tests/unit/test_orm_knobs.py）。

    集中在本檔的理由：兩個旋鈕都在本檔生效（``_orm_executor`` 由
    ``ORM_THREADPOOL_SIZE`` 建立、連線重用由 ``CONN_MAX_AGE`` 決定），而 controller
    不該為了回報設定值去讀 ``django.conf``——鐵則 3 的 endpoint 不碰基礎設施。

    **刻意不含 DB 主機與埠。** 那是內部拓撲，而診斷端點通常無認證（存活探測要能
    在沒有憑證的情況下打），回報等於公開基礎設施位置（鐵則 9）。

    回報的是**設定值**。「設定值 == 真正生效的值」目前靠 ``_orm_executor`` 在
    import 期由同一個 setting 建立來保證，本函式不另外去讀 executor 的實際寬度
    （那要碰私有屬性）。若日後 executor 改成延遲建立或可替換，這裡必須改成讀實際
    值——否則這支端點就失去它存在的理由（見下方 docstring 引用的量測教訓）。
    """
    return {
        "conn_max_age": settings.DATABASES["default"]["CONN_MAX_AGE"],
        "orm_threadpool_size": int(settings.ORM_THREADPOOL_SIZE),
    }

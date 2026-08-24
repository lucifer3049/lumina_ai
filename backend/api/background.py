"""背景生成的登記表與 graceful shutdown（11 §196、1D-4b）。

1D-4a 把生成搬到背景 task 上——那是 G-06（client 斷線後繼續收完）與「重送不會變兩份」
的前提。代價是本專案第一次出現「HTTP 請求結束之後仍然有事情在跑」的形狀，而**部署重啟
落在那些事情上的後果**就是這個模組要處理的。

登記表存在的兩個理由：

1. **強參考**。`asyncio` 只持有 task 的弱參考，沒有人拿著它的話，跑到一半的 task 可能
   被 GC 掉——症狀是「偶爾有一則訊息永遠停在 streaming」，而它重現不了。
2. **關機時知道還有誰在跑**。11 §196：SIGTERM → 停收新請求 → SSE 送 `error(retryable)`
   → 等待 ≤30s → 退出。不等的話進行中的回答直接蒸發，而資料庫裡那一則永遠是
   `streaming`；無限期等的話，一個卡住的 provider 會讓整個部署停在那裡，而 K8s 在
   寬限期之後一律 SIGKILL——結果與不等相同，只是先浪費了幾分鐘。

**放在 `api/` 而不是 `core/`**：它管的是 HTTP 行程的生命週期。Celery worker 有自己的
關機機制（`warm shutdown`，08 §6），兩者不共用。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from core.exceptions import ServerBusyError

logger = get_logger(__name__)

__all__ = [
    "SHUTDOWN_DRAIN_SECONDS",
    "drain",
    "ensure_capacity",
    "pending_count",
    "spawn",
]

# 11 §196：等待 ≤30s。**要與部署的 `terminationGracePeriodSeconds` 對得上**——比它長
# 的話，等待會在中途被 SIGKILL 打斷，這個上限就等於不存在。
SHUTDOWN_DRAIN_SECONDS = 30

_running: set[asyncio.Task[None]] = set()


def ensure_capacity() -> None:
    """這個行程的背景生成還有名額嗎？沒有就 429（二次架構審計 F-04）。

    **每租戶的 `streams` 額度擋不住這件事**：它是公平性機制（一個租戶最多同時開幾
    條），而租戶數不設限——N 個租戶各開滿就是 N×2 條，沒有上界。`spawn()` 在此之前
    是無條件 `create_task`，所以「一個行程能同時扛幾條生成」沒有答案。超載的症狀
    不是有人被擋下，是**全部一起變慢**：每條生成吃一個 LLM 連線、一份 context、
    一條 SSE 緩衝，而 11 §2 的 TTFT p95 在那個點之後不再有意義。

    **擋在建立回合之前呼叫**（見 `api/v1/conversations.py`）：擋在後面的話，被拒的
    請求已經寫了兩則訊息、扣了三種額度，而使用者拿到的是 429——那比不擋更糟。

    **check-then-spawn 之間有一個微秒級的窗**，兩個請求可能同時看到最後一個名額。
    這裡刻意不上鎖：這是一道粗粒度的過載保護，超收一兩條的代價遠小於在每次送訊息
    的路徑上加一個同步原語。真正的精確擋線是租戶額度那一層。

    設定值 ≤ 0 視為不設限——緊急時的退路（改一個環境變數就回到 2B 之前的行為）。
    """
    limit = get_app_settings().api_max_concurrent_generations
    if limit <= 0 or len(_running) < limit:
        return
    retry_after = get_app_settings().api_busy_retry_after_seconds
    logger.warning("generation_capacity_reached", running=len(_running), limit=limit)
    raise ServerBusyError(retry_after_seconds=retry_after)


def spawn(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    """把一個生成丟到背景跑，並登記起來。

    容量檢查**不在這裡**（在 `ensure_capacity()`，由呼叫端在建立回合之前呼叫）：
    這個函式已經拿到一個 coroutine，此時拒絕等於把它丟掉，而那個 coroutine 背後
    是一個已經寫進 DB 的回合。
    """
    task = asyncio.create_task(coro)
    _running.add(task)
    task.add_done_callback(_running.discard)
    task.add_done_callback(_log_failure)
    return task


def _log_failure(task: asyncio.Task[None]) -> None:
    """背景 task 的例外要有人記——**沒有人在 await 它**。

    `ChatService.generate` 設計上不往外拋（失敗一律走完整的收尾路徑），所以正常情況
    這裡什麼都不會做。走得到的是「收尾路徑自己炸了」：DB 在寫最終訊息時斷線、Redis
    在送 error 事件時掛掉。那時例外會停在 task 物件裡，直到 GC 才以
    ``Task exception was never retrieved`` 冒出來——沒有 request_id、沒有 tenant、
    位置指向 GC 發生的地方，而那通常是別的請求正在跑的時候。

    取消不算失敗：關機的 `drain()` 逾時後會取消剩下的，那是預期行為。
    """
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        logger.error("background_task_failed", exc_info=exception)


def pending_count() -> int:
    return len(_running)


async def drain(*, timeout_seconds: float = SHUTDOWN_DRAIN_SECONDS) -> int:
    """等進行中的生成收工；回傳**逾時後仍未收完**的數量。

    回數量而不是拋例外：呼叫端是關機流程，它唯一能做的事是把這個數字記進日誌——
    那是事後判斷「這次重啟弄丟了幾個回答」的唯一依據。

    逾時之後**取消**剩下的，不是放著：放著的話，event loop 關閉時會丟出一串
    `Task was destroyed but it is pending!`，而那段噪音正好蓋掉關機時真正該看的訊息。
    """
    pending = set(_running)
    if not pending:
        return 0

    _, unfinished = await asyncio.wait(pending, timeout=timeout_seconds)
    for task in unfinished:
        task.cancel()
    if unfinished:
        logger.warning("generation_drain_timed_out", unfinished=len(unfinished))
    return len(unfinished)

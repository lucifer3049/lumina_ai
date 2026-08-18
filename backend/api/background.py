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

logger = get_logger(__name__)

__all__ = ["SHUTDOWN_DRAIN_SECONDS", "drain", "pending_count", "spawn"]

# 11 §196：等待 ≤30s。**要與部署的 `terminationGracePeriodSeconds` 對得上**——比它長
# 的話，等待會在中途被 SIGKILL 打斷，這個上限就等於不存在。
SHUTDOWN_DRAIN_SECONDS = 30

_running: set[asyncio.Task[None]] = set()


def spawn(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    """把一個生成丟到背景跑，並登記起來。"""
    task = asyncio.create_task(coro)
    _running.add(task)
    task.add_done_callback(_running.discard)
    return task


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

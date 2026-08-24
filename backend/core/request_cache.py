"""請求級的記憶體 memo —— 同一個請求內重複問同一件事只查一次（二次架構審計 F-03）。

**這不是快取層。** 沒有 TTL、不跨請求、不進 Redis：它的整個生命週期就是一個請求
（或一個 Celery task、一次腳本執行）。跨請求的快取要面對失效、雪崩、租戶隔離三個
問題；這裡一個都沒有，因為出了那個 `with` 區塊資料就不存在了。

**為什麼需要它**：`QuotaService.limits()` 每次呼叫都開一組
`tenant_context + unit_of_work`（= 一次交易）。而一則聊天訊息的 `start_turn` 會
連續呼叫三次 `check_and_reserve`（messages_day / tokens_month / streams），上傳
路徑是兩次 `limits` + 兩次 DB 聚合——同一個租戶、同一份限額表，在同一個請求裡查
三到四遍。限額在請求中途不會變，多出來的那幾趟純粹是成本，而它落在 TTFT 的量測
路徑上（11 §1.1）。

**放 `core/` 而不是 `api/`**：讀者是 `services/`（鐵則 2：service 不得 import api）。
區塊由 `api/middleware/request_context.py` 開，那是每個請求都會經過的最外層。

**沒有進入區塊時一律不快取**（`cached()` 直接呼叫 `produce`）。Celery task、
管理指令、測試都走這條路——它們沒有「請求」這個邊界，而在沒有邊界的地方快取
等於做出一個永遠不會失效的全域變數。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, cast

__all__ = ["cached", "request_cache"]

# `None` = 不在請求區塊內。用 None 而不是空 dict 來表達「沒有邊界」：後者分不出
# 「請求內、還沒存東西」與「根本不在請求裡」，而兩者的正確行為相反。
_cache: ContextVar[dict[str, Any] | None] = ContextVar("request_cache", default=None)


@contextmanager
def request_cache() -> Iterator[None]:
    """開一個請求級的 memo 區塊；離開時整份丟掉。

    用 `ContextVar` 的 token 還原而不是設回 None：巢狀進入時（測試、或未來的
    子請求）內層結束後要回到外層那一份，設 None 會把外層的也一起清掉。
    """
    token = _cache.set({})
    try:
        yield
    finally:
        _cache.reset(token)


def cached[T](key: str, produce: Callable[[], T]) -> T:
    """`key` 在本請求內算過就回上次的值，否則呼叫 `produce` 並記下來。

    **key 必須含租戶**（呼叫端負責）：memo 是以 contextvar 存活的，而 contextvar
    會被 `spawn()` 出去的背景 task 繼承——同一份 dict 若被不同租戶共用，回答會
    帶著別人的限額。這一層不強制，因為它不認識租戶（`core/tenant.py` 才認識），
    但每個呼叫端的 key 都要能通過「換一個租戶會不會撞到同一格」這個問題。

    **不快取例外**：`produce` 拋出時什麼都不記，下一次呼叫會再試一遍。記下失敗
    等於把一次暫時的 DB 抖動變成整個請求都失敗。
    """
    store = _cache.get()
    if store is None:
        return produce()
    if key in store:
        return cast("T", store[key])
    value = produce()
    store[key] = value
    return value

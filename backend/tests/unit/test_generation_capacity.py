"""驗收：背景生成的行程級上限（`api/background.ensure_capacity`；二次架構審計 F-04）。

**每租戶的 `streams` 額度擋不住這件事**：那是公平性機制（一個租戶同時最多幾條），
而租戶數不設限——N 個租戶各開滿就是 N×2，沒有上界。超載的症狀不是有人被擋下，是
全部一起變慢，而 11 §2 的 TTFT p95 在那個點之後不再有意義。

三件事錯了都不會有錯誤訊息：

1. **擋的時機**。擋在建立回合之後的話，被拒的請求已經寫了兩則訊息、扣了三種額度，
   而使用者拿到的是 429——那比不擋更糟。時機由
   `tests/api/test_chat_capacity.py` 從端點那一側驗。
2. **名額沒有歸還**。task 結束後若還算在裡面，行程會在跑滿一次之後永久拒絕服務。
3. **錯誤碼**。用 `QUOTA_EXCEEDED` 的話，client 會把一個「等五秒就好」的情況當成
   「這一期的額度用完了」而放棄重試。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from api.background import ensure_capacity, pending_count, spawn
from config.settings.app_settings import get_app_settings
from core.exceptions import ErrorCode, ServerBusyError

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def fresh_settings() -> Iterator[None]:
    """設定是 lru_cache 的；改過環境變數的測試不清快取會污染後面的測試。"""
    get_app_settings.cache_clear()
    yield
    get_app_settings.cache_clear()


async def _occupy(slots: int) -> asyncio.Event:
    """佔滿 `slots` 個名額；回傳放行用的 event（測試結束一定要 set）。"""
    release = asyncio.Event()
    for _ in range(slots):
        spawn(release.wait())  # type: ignore[arg-type]
    await asyncio.sleep(0)
    return release


class TestCapacityGate:
    async def test_it_lets_traffic_through_below_the_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("API_MAX_CONCURRENT_GENERATIONS", "3")
        get_app_settings.cache_clear()
        release = await _occupy(2)
        try:
            ensure_capacity()  # 不該拋
        finally:
            release.set()

    async def test_it_refuses_once_the_process_is_full(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("API_MAX_CONCURRENT_GENERATIONS", "2")
        get_app_settings.cache_clear()
        release = await _occupy(2)
        try:
            with pytest.raises(ServerBusyError) as caught:
                ensure_capacity()
        finally:
            release.set()

        assert caught.value.code is ErrorCode.RATE_LIMITED, (
            "不能用 QUOTA_EXCEEDED：那是「這一期額度用完了、重試無用」，而這裡原封不動重送就會成功"
        )
        assert caught.value.details["retry_after_seconds"] > 0, (
            "429 不附重試時間等於叫 client 自己猜，而猜出來的多半是「立刻重試」"
        )

    async def test_a_finished_generation_gives_its_slot_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """沒有歸還的話，行程跑滿一次之後就永久拒絕服務——而它不會有任何錯誤。"""
        monkeypatch.setenv("API_MAX_CONCURRENT_GENERATIONS", "1")
        get_app_settings.cache_clear()
        release = await _occupy(1)
        with pytest.raises(ServerBusyError):
            ensure_capacity()

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert pending_count() == 0
        ensure_capacity()

    async def test_zero_means_unlimited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """緊急時的退路：改一個環境變數就回到 2B 之前的行為。"""
        monkeypatch.setenv("API_MAX_CONCURRENT_GENERATIONS", "0")
        get_app_settings.cache_clear()
        release = await _occupy(5)
        try:
            ensure_capacity()
        finally:
            release.set()

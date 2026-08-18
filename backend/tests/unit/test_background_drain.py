"""驗收：背景生成的登記與 graceful shutdown（11 §196、13 §3 工作包 1D-4b）。

1D-4a 把生成搬到背景 task 上（那是 G-06 與「重送不會變兩份」的前提），但也因此第一次
出現了「HTTP 請求結束之後仍然有事情在跑」的形狀。**部署重啟落在那些事情上的後果，是
這一包要處理的最後一件事。**

11 §196 的順序：SIGTERM → 停收新請求 → SSE 送 `error(retryable)` 並等待 ≤30s → 退出。
三個部分各自對應一種失敗：

1. **不等就退出**：進行中的回答直接蒸發。使用者的畫面停在半句話，而資料庫裡那一則
   永遠是 `streaming`——重整也不會變，因為沒有人會再去動它。
2. **無限期等待**：一個卡住的 provider 會讓整個部署卡住，而 K8s 在寬限期之後一律
   SIGKILL——結果與第 1 種相同，只是先浪費了幾分鐘。
3. **等完了卻不說一聲**：client 會一直等下一個事件直到自己逾時，而它本來可以立刻
   顯示「生成中斷，請重試」（09 附錄 A 的 retryable）。

本檔是純邏輯（不碰 DB、Redis 與 HTTP）：登記表本身要正確，關機時的行為才有意義。
"""

from __future__ import annotations

import asyncio

import pytest

from api.background import SHUTDOWN_DRAIN_SECONDS, drain, pending_count, spawn


@pytest.fixture(autouse=True)
async def _clean_registry() -> None:
    """每條測試都從空的登記表開始——上一條留下的 task 會讓計數斷言隨機失敗。"""
    await drain(timeout_seconds=5)


class TestRegistry:
    async def test_a_spawned_task_is_tracked(self) -> None:
        """**登記是為了強參考**：asyncio 只持有弱參考，沒有登記的 task 可能在跑完之前
        被 GC 掉，而症狀是「偶爾有一則訊息永遠停在 streaming」——重現不了。"""

        async def _work() -> None:
            await asyncio.sleep(0.05)

        spawn(_work())

        assert pending_count() == 1

    async def test_it_forgets_finished_tasks(self) -> None:
        """跑完就從登記表移除。不移除的話，一個長時間執行的行程會累積掉每一次對話的
        task 物件——那是一個只在正式環境、跑很久之後才看得出來的洩漏。"""

        async def _work() -> None:
            return None

        spawn(_work())
        await drain(timeout_seconds=5)

        assert pending_count() == 0

    async def test_a_crashing_task_does_not_break_the_registry(self) -> None:
        """背景 task 的例外沒有人接得住。登記表**不能**因此壞掉，否則一次崩潰會讓
        之後每一次關機都卡在同一筆上。"""

        async def _boom() -> None:
            raise RuntimeError("炸了")

        spawn(_boom())
        await drain(timeout_seconds=5)

        assert pending_count() == 0


class TestDrain:
    async def test_it_waits_for_work_in_flight(self) -> None:
        """關機要等進行中的生成收工——那正是 G-06 的「成本已經發生，收完並存好」。"""
        done = False

        async def _work() -> None:
            nonlocal done
            await asyncio.sleep(0.05)
            done = True

        spawn(_work())
        await drain(timeout_seconds=5)

        assert done

    async def test_it_gives_up_after_the_deadline(self) -> None:
        """**等待要有上限。** 一個卡住的 provider 會讓整個部署停在那裡，而 K8s 在寬限
        期之後一律 SIGKILL——結果與不等一樣，只是先浪費了幾分鐘。"""

        async def _stuck() -> None:
            await asyncio.sleep(30)

        spawn(_stuck())

        remaining = await drain(timeout_seconds=0.05)

        assert remaining == 1, "逾時要回報還有幾個沒收完（給關機日誌用）"

    async def test_it_cancels_what_it_gave_up_on(self) -> None:
        """放棄等待之後要**取消**它們，不是放著。

        放著的話，event loop 關閉時會丟出一串
        `Task was destroyed but it is pending!`，而那段噪音正好蓋住關機時真正該看的
        訊息（哪些生成沒收完）。
        """
        cancelled = False

        async def _stuck() -> None:
            nonlocal cancelled
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled = True
                raise

        spawn(_stuck())
        await drain(timeout_seconds=0.05)
        await asyncio.sleep(0)  # 讓取消真的送達

        assert cancelled

    async def test_draining_an_empty_registry_is_instant(self) -> None:
        """沒有進行中的生成時，關機不該多花任何時間（絕大多數的部署都是這種情況）。"""
        assert await drain(timeout_seconds=5) == 0

    def test_the_deadline_matches_the_spec(self) -> None:
        """11 §196：等待 ≤30s。它要與部署的 `terminationGracePeriodSeconds` 對得上
        ——比它長的話，等待會被 SIGKILL 打斷，那個上限就等於不存在。"""
        assert SHUTDOWN_DRAIN_SECONDS == 30


class TestLifespanWiring:
    async def test_shutdown_drains(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """**登記表正確但沒有人在關機時呼叫它，等於什麼都沒做。**

        這條把 `create_app()` 的 lifespan 與 `drain()` 綁在一起：拿掉那一行時，上面
        所有測試仍然全綠，而正式環境的重啟會繼續蒸發進行中的生成。
        """
        called: list[float] = []

        async def _spy(*, timeout_seconds: float = SHUTDOWN_DRAIN_SECONDS) -> int:
            called.append(timeout_seconds)
            return 0

        monkeypatch.setattr("api.main.drain", _spy)

        from api.main import _lifespan, create_app

        app = create_app()
        async with _lifespan(app):
            pass

        assert called == [SHUTDOWN_DRAIN_SECONDS]

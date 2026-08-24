"""驗收：請求級 memo（`core/request_cache.py`；二次架構審計 F-03）。

它存在的唯一理由是省掉重複的交易：`QuotaService.limits()` 在一則聊天訊息裡會被叫
三次（messages_day / tokens_month / streams），每次都開一組
`tenant_context + unit_of_work`。

**三個錯法都不會有錯誤訊息**：

1. **沒有邊界時仍然快取**。Celery task 與管理指令沒有「請求」這回事，在那裡快取
   等於做出一個永遠不會失效的全域變數——那個 worker 行程會一直用著它第一次讀到的
   限額，改了方案也不生效。
2. **跨請求外流**。上一個請求的值留給下一個請求用，而兩個請求可能是不同租戶——
   症狀是額度看起來是別人的。
3. **快取了例外**。一次 DB 抖動被記下來，整個請求後續每一次呼叫都拿到同一個失敗。
"""

from __future__ import annotations

import pytest

from core.request_cache import cached, request_cache


class _Counter:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return self.calls


class TestInsideARequest:
    def test_the_second_ask_does_not_recompute(self) -> None:
        produce = _Counter()

        with request_cache():
            first = cached("k", produce)
            second = cached("k", produce)

        assert (first, second) == (1, 1)
        assert produce.calls == 1

    def test_different_keys_are_independent(self) -> None:
        """key 含租戶（呼叫端負責）。同一格會讓一個租戶讀到另一個租戶的限額。"""
        produce = _Counter()

        with request_cache():
            assert cached("tenant-a", produce) == 1
            assert cached("tenant-b", produce) == 2

    def test_a_failure_is_not_remembered(self) -> None:
        """記下失敗等於把一次暫時的抖動變成整個請求都失敗。"""
        attempts = 0

        def flaky() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("暫時的")
            return "ok"

        with request_cache():
            with pytest.raises(RuntimeError):
                cached("k", flaky)

            assert cached("k", flaky) == "ok"


class TestOutsideARequest:
    def test_nothing_is_cached_without_a_boundary(self) -> None:
        """Celery task、管理指令、腳本都走這條路——它們的「一次」可能跨越好幾分鐘。"""
        produce = _Counter()

        assert cached("k", produce) == 1
        assert cached("k", produce) == 2

    def test_the_store_does_not_survive_the_block(self) -> None:
        """離開區塊就沒了——否則下一個請求（可能是別的租戶）會讀到上一個的值。"""
        produce = _Counter()

        with request_cache():
            cached("k", produce)

        assert cached("k", produce) == 2

    def test_nested_blocks_restore_the_outer_store(self) -> None:
        """用 token 還原而不是設回 None：後者會把外層那一份一起清掉。"""
        produce = _Counter()

        with request_cache():
            outer = cached("k", produce)
            with request_cache():
                assert cached("k", produce) == 2, "內層是新的一份"
            assert cached("k", produce) == outer, "回到外層那一份"

"""驗收：終局事件之後縮短 SSE 緩衝區壽命（二次架構審計 L1）。

`BUFFER_TTL_SECONDS` 的 5 分鐘是為了「生成還在跑」——一條長回答不能在中途過期。
可是終局事件送出之後，緩衝區只剩一個用途：**client 在收到 `done` 之前就斷線了，
重連回來補讀最後幾個事件**，而那只會發生在幾秒內。於是每一條串流的完整回答都在
Redis 躺滿 5 分鐘，其中 4 分多鐘沒有讀者。

`StreamBuffer.drop()` 的 docstring 從 1D-4b 起就寫著這是「200 併發下的記憶體差別」，
而**它一個呼叫端都沒有**——程式碼承諾的最佳化沒有被接線。

**兩個錯法方向相反，都要擋**：

1. **縮得太早**（在最後一個 `append` 之前呼叫）。`append` 每次都把 TTL 重設回 5 分鐘，
   所以順序反過來就是白做——而它不會有任何症狀，Redis 水位照樣高。
2. **直接刪掉**。那會把「斷線後回來續傳」變成 409 `RESUME_EXPIRED`：一個成本問題
   被換成使用者看得見的錯誤。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from config.settings.app_settings import get_app_settings
from core.redis import get_redis
from core.streams import BUFFER_TTL_SECONDS, StreamBuffer, settled_ttl_seconds
from tests.conftest import TENANT_A

pytestmark = pytest.mark.asyncio


@pytest.fixture
def buffer() -> Iterator[StreamBuffer]:
    buf = StreamBuffer(tenant_id=TENANT_A, message_id=uuid.uuid4())
    yield buf
    client = get_redis()
    client.delete(buf.key, f"{buf.key}:stop")


def _ttl(key: str) -> int:
    """剩餘秒數。`-1` = 永不過期、`-2` = 不存在。"""
    return int(get_redis().ttl(key))  # type: ignore[arg-type]


class TestWhileGenerating:
    async def test_each_append_keeps_the_long_window(self, buffer: StreamBuffer) -> None:
        """**這是不能被 L1 弄壞的前提**：一條跑了六分鐘的長回答不能在中途過期，
        症狀會是「長回答的 resume 一定失敗、短回答都正常」。"""
        await buffer.append("delta", {"text": "一"})
        await buffer.append("delta", {"text": "二"})

        assert _ttl(buffer.key) > settled_ttl_seconds()
        assert _ttl(buffer.key) <= BUFFER_TTL_SECONDS


class TestAfterTheTerminalEvent:
    async def test_settle_shortens_the_window(self, buffer: StreamBuffer) -> None:
        await buffer.append("delta", {"text": "一"})
        await buffer.append("done", {"finish_reason": "stop"})

        await buffer.settle()

        assert 0 < _ttl(buffer.key) <= settled_ttl_seconds()

    async def test_the_stop_flag_expires_with_it(self, buffer: StreamBuffer) -> None:
        """旗標與緩衝區同生共死。留著一個沒有緩衝區的旗標，只會讓下一次
        `stop_requested()` 多一次 round trip。"""
        await buffer.append("delta", {"text": "一"})
        await buffer.request_stop()

        await buffer.settle()

        assert 0 < _ttl(f"{buffer.key}:stop") <= settled_ttl_seconds()

    async def test_the_events_are_still_readable(self, buffer: StreamBuffer) -> None:
        """**縮短不是刪除。** 直接 `drop()` 會讓正在重連的 client 拿到 409
        `RESUME_EXPIRED`——把一個成本問題換成使用者看得見的錯誤。"""
        await buffer.append("delta", {"text": "一"})
        await buffer.append("done", {"finish_reason": "stop"})
        await buffer.settle()

        assert [event.type for event in await buffer.history()] == ["delta", "done"]
        assert await buffer.exists()


class TestOrderingMatters:
    async def test_an_append_after_settle_undoes_it(self, buffer: StreamBuffer) -> None:
        """釘住 `settle()` 必須在最後一個 `append` 之後的理由。

        順序反過來不會報錯、緩衝區內容也完全正確——只有 Redis 水位知道白做了。
        這條測試把那件看不見的事變成看得見的。
        """
        await buffer.append("done", {"finish_reason": "stop"})
        await buffer.settle()
        await buffer.append("citations", {"items": []})

        assert _ttl(buffer.key) > settled_ttl_seconds(), (
            "append 把 TTL 推回 5 分鐘了——settle 必須是最後一步"
        )


class TestItIsConfigurable:
    async def test_the_window_comes_from_settings(
        self, buffer: StreamBuffer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """可調參數不寫死（CLAUDE.md 鐵則 9 的同一條紀律）。"""
        monkeypatch.setenv("STREAM_SETTLED_TTL_SECONDS", "5")
        get_app_settings.cache_clear()
        try:
            await buffer.append("done", {"finish_reason": "stop"})
            await buffer.settle()

            assert 0 < _ttl(buffer.key) <= 5
        finally:
            get_app_settings.cache_clear()

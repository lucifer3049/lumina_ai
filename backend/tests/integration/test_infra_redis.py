"""驗收：Redis 是否照 12 §4.1 / 11 §4.1 / 01 附錄 A 的規格起來。

Redis 在本專案被明確定位為「可重建快取」（12 §4.1：可丟，quota 以 DB 對帳恢復），
但那是**資料可丟**，不是**設定可隨便**：

- `appendonly` / `appendfsync everysec` 是 12 §4.1 寫死的；關掉之後重啟會丟掉
  SSE resume buffer 與 rate limit 計數，症狀是零星、難重現的使用者端錯誤。
- `requirepass`：compose 網路內沒有預設認證等於任何容器都能讀全租戶的 quota 與
  session denylist（10 安全設計）。這條測試存在的意義是「沒有密碼會紅燈」。
- client timeout 500ms 出自 11 §4.1 全域字典；CLAUDE.md 要求所有對外呼叫必有
  timeout，這裡驗證的是「宣告值沒被改掉」，不是連線本身。
"""

from __future__ import annotations

from typing import Any, cast

import pytest
import redis

from config.settings.app_settings import get_app_settings

# 11 §4.1 Timeout 全域字典：Redis 500ms。
REDIS_TIMEOUT_SECONDS = 0.5


@pytest.fixture
def redis_client() -> redis.Redis:
    settings = get_app_settings()
    return redis.Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_timeout=settings.redis_timeout_seconds,
        socket_connect_timeout=settings.redis_timeout_seconds,
    )


def test_redis_reachable(redis_client: redis.Redis) -> None:
    assert redis_client.ping() is True


def test_persistence_is_aof_everysec(redis_client: redis.Redis) -> None:
    """AOF 必開且 fsync 策略為 everysec（12 §4.1）。"""
    # cast 的理由同 services/identity/auth.py：redis-py 6 的命令回傳型別是
    # ``Awaitable | Any``（同步與非同步 client 共用宣告）。
    config = cast(dict[str, Any], redis_client.config_get("appendonly")) | cast(
        dict[str, Any], redis_client.config_get("appendfsync")
    )

    assert config.get("appendonly") == "yes", "appendonly 未開（12 §4.1）"
    assert config.get("appendfsync") == "everysec", (
        f"appendfsync 是 {config.get('appendfsync')}，規格是 everysec（12 §4.1）"
    )


def test_unauthenticated_connection_is_rejected() -> None:
    """沒帶密碼必須連不上——證明 requirepass 真的生效，不是設定寫了沒讀到。"""
    settings = get_app_settings()
    anonymous = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        socket_timeout=REDIS_TIMEOUT_SECONDS,
        socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
    )

    with pytest.raises(redis.exceptions.AuthenticationError):
        anonymous.ping()


def test_client_timeout_matches_spec() -> None:
    """宣告值對帳：11 §4.1 全域字典 Redis 500ms。"""
    assert get_app_settings().redis_timeout_seconds == REDIS_TIMEOUT_SECONDS


class TestAsyncClientTimeouts:
    """SSE 那條路徑（1D-4a）的 async client：**只有 XREAD BLOCK 可以沒有逾時。**

    `get_async_redis()` 當初刻意不設 `socket_timeout`，理由對——但 `core/streams.py`
    的 append／exists／set／ttl／delete 全走同一個 client，而那些是幾毫秒該回來的
    指令。Redis 半死不活時它們會無限期掛在 event loop 上的 SSE coroutine 裡，症狀是
    「所有串流都卡住不吐字」，完全不指向 Redis（CLAUDE.md：所有對外呼叫必有 timeout）。
    """

    @staticmethod
    def _socket_timeout(client: Any) -> float | None:
        timeout: float | None = client.connection_pool.connection_kwargs.get("socket_timeout")
        return timeout

    async def test_the_default_client_has_a_socket_timeout(self) -> None:
        from core.redis import get_async_redis

        assert self._socket_timeout(get_async_redis()) == REDIS_TIMEOUT_SECONDS

    async def test_the_blocking_client_deliberately_has_none(self) -> None:
        """`XREAD BLOCK` 的等待不是故障——設了逾時等於把它該做的事當成錯誤中斷。"""
        from core.redis import get_async_redis

        assert self._socket_timeout(get_async_redis(blocking=True)) is None

    async def test_they_are_two_different_clients(self) -> None:
        """同一個 client 不可能同時是兩種逾時；分開才是這條規則的落地點。"""
        from core.redis import get_async_redis

        assert get_async_redis() is not get_async_redis(blocking=True)
        assert get_async_redis() is get_async_redis()  # 每個 loop 仍只有一份

    async def test_connect_timeout_is_set_on_both(self) -> None:
        """連不上與等不到是兩件事：前者在任何情況下都該有上限。"""
        from core.redis import get_async_redis

        for client in (get_async_redis(), get_async_redis(blocking=True)):
            kwargs = client.connection_pool.connection_kwargs
            assert kwargs["socket_connect_timeout"] == REDIS_TIMEOUT_SECONDS

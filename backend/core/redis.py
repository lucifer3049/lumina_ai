"""Redis 連線與 key 命名 —— 全 repo 唯一的 Redis 入口。

**租戶前綴強制在這一層**（鐵則 4）：key 一律 ``t:{tenant_id}:...``。前綴如果留給
呼叫端自己記得加，遲早有人漏掉，而漏掉的後果是兩個租戶共用同一個計數器或撤銷
名單——例如 A 租戶的登入失敗次數把 B 租戶的帳號鎖住，或 A 的登出讓 B 的 token
失效。這類錯誤不會有例外，只會是「偶爾發生的怪現象」。

**同步 client 而非 asyncio**：本專案的 service 層是同步的（ADR-001：Django ORM
同步，async endpoint 一律經 ``run_orm`` 進 threadpool）。Redis 呼叫發生在同一條
threadpool 執行緒上，所以同步 client 不會阻塞 event loop；混用兩套 client 反而
會讓「這段程式跑在哪一側」變得難以推理。

連線池由 redis-py 內建管理，模組層單例即可——每次 ``Redis(...)`` 都會建一個新池。
"""

from __future__ import annotations

import uuid
from functools import lru_cache

from redis import Redis

from config.settings.app_settings import get_app_settings


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    """取共用 client。

    ``socket_timeout`` 出自 11 §4.1（Redis 500ms）：所有對外呼叫必有 timeout，
    否則 Redis 卡住時整個 threadpool 會被慢慢佔滿，症狀是「網站整體變慢」而不是
    「Redis 有問題」。
    """
    settings = get_app_settings()
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_timeout=settings.redis_timeout_seconds,
        socket_connect_timeout=settings.redis_timeout_seconds,
        decode_responses=True,
    )


def tenant_key(tenant_id: uuid.UUID, *parts: str) -> str:
    """組出帶租戶前綴的 key：``t:{tenant_id}:part1:part2``。

    ``*parts`` 允許 ``"*"`` 之類的樣式字元，測試會用它掃自己造出來的 key。
    """
    return ":".join(("t", str(tenant_id), *parts))

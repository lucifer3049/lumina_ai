"""Redis 連線與 key 命名 —— 全 repo 唯一的 Redis 入口。

**租戶前綴強制在這一層**（鐵則 4）：key 一律 ``t:{tenant_id}:...``。前綴如果留給
呼叫端自己記得加，遲早有人漏掉，而漏掉的後果是兩個租戶共用同一個計數器或撤銷
名單——例如 A 租戶的登入失敗次數把 B 租戶的帳號鎖住，或 A 的登出讓 B 的 token
失效。這類錯誤不會有例外，只會是「偶爾發生的怪現象」。

**同步 client 而非 asyncio**：本專案的 service 層是同步的（ADR-001：Django ORM
同步，async endpoint 一律經 ``run_orm`` 進 threadpool）。Redis 呼叫發生在同一條
threadpool 執行緒上，所以同步 client 不會阻塞 event loop；混用兩套 client 反而
會讓「這段程式跑在哪一側」變得難以推理。

**例外是跑在 event loop 上的 transport 層，見 :func:`get_async_redis`**：SSE 串流
（1D-4a）與認證前的 rate limit middleware（2026-08-30 深度審查修正）。SSE 要「等
下一個事件到達」，那個等待若發生在 threadpool 上就是一條串流佔一條執行緒——11 §26
的容量規劃是每個 replica 200 條併發串流，而 §45 明寫「SSE 為 IO-bound，async 原生
擅長」；middleware 則根本沒有 threadpool 可躲，同步 client 的每一次呼叫都是 loop
上的阻塞 I/O。上面那條規則的理由（讓「跑在哪一側」可推理）在這兩處反而指向
async：它們的每一段都跑在 event loop 上。

連線池由 redis-py 內建管理，模組層單例即可——每次 ``Redis(...)`` 都會建一個新池。
"""

from __future__ import annotations

import uuid
import weakref
from asyncio import AbstractEventLoop, get_running_loop
from functools import lru_cache

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.commands.core import Script

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


# 一個 event loop 一個 client（見 `get_async_redis`），而且**阻塞與非阻塞各一個**
# ——兩者的 `socket_timeout` 必須不同，理由見 `get_async_redis`。鍵是
# `(loop, blocking)`。**弱參考鍵**：loop 結束後這一筆要能被回收，否則測試每跑一條
# 就留下一個永遠不會再用的連線池。
_async_clients: weakref.WeakKeyDictionary[AbstractEventLoop, dict[bool, AsyncRedis]] = (
    weakref.WeakKeyDictionary()
)


def get_async_redis(*, blocking: bool = False) -> AsyncRedis:
    """event loop 上的 transport 層專用（SSE 串流、rate limit middleware）。
    **service 層與其他地方一律用 `get_redis()`**（模組 docstring 的分工）。

    **不是模組層單例，而是「每個 event loop 一個」**：`redis.asyncio` 的連線與
    `asyncio.Future` 綁在建立它的那個 loop 上，跨 loop 使用會丟
    ``got Future attached to a different loop``。正式環境只有一個 loop，所以這與
    單例等價；但測試每一條都跑在自己的 loop 上，單例會在第二條測試就炸——而那個
    錯誤訊息完全不會指向這裡。

    **`blocking` 決定有沒有 `socket_timeout`，而預設是「有」**：

    - ``blocking=True`` 只給 ``XREAD BLOCK`` 用。那條指令的用途就是「沒有事件時就
      等著」，設了逾時等於把它該做的事當成故障中斷；逾時改由 ``block_ms`` 在每次
      呼叫上決定（core/streams.py 的 `StreamBuffer.follow`）。
    - 其餘指令（XADD／XRANGE／EXISTS／SET／TTL／DEL）都是幾毫秒該回來的東西，
      一律走有逾時的那一個。少了它，Redis 半死不活時這些呼叫會**無限期**掛在
      event loop 上的 SSE coroutine 裡——症狀是「所有串流都卡住不吐字」，而那不會
      指向 Redis（CLAUDE.md：所有對外呼叫必有 timeout）。

    分成兩個 client 而不是在呼叫端包 `asyncio.timeout`：逾時由 redis-py 自己偵測時，
    它知道要把那條連線丟掉；從外面取消一個進行到一半的指令，連線上還留著沒讀完的
    回應，下一個借到它的呼叫會拿到別人的答案。
    """
    loop = get_running_loop()
    per_loop = _async_clients.setdefault(loop, {})
    client = per_loop.get(blocking)
    if client is None:
        settings = get_app_settings()
        client = AsyncRedis.from_url(
            settings.redis_url.get_secret_value(),
            socket_timeout=None if blocking else settings.redis_timeout_seconds,
            socket_connect_timeout=settings.redis_timeout_seconds,
            decode_responses=True,
        )
        per_loop[blocking] = client
    return client


def tenant_key(tenant_id: uuid.UUID, *parts: str) -> str:
    """組出帶租戶前綴的 key：``t:{tenant_id}:part1:part2``。

    ``*parts`` 允許 ``"*"`` 之類的樣式字元，測試會用它掃自己造出來的 key。
    """
    return ":".join(("t", str(tenant_id), *parts))


@lru_cache(maxsize=8)
def get_script(lua: str) -> Script:
    """註冊一段 Lua，回傳可直接呼叫的 :class:`Script`（走 EVALSHA，sha 不在時自動
    退回 EVAL 並重新載入）。

    **需要原子性的多步驟操作一律走這裡。** Redis 的單一命令是原子的，但「讀出來、
    依結果再寫回去」不是——中間擠進另一個請求時，兩邊會依同一份舊值各做一次決定，
    而那類錯誤只在併發下出現、事後從資料看不出發生過什麼
    （`services/identity/auth.py` 的 refresh 輪換就是這個形狀）。

    以 Lua 原文為快取鍵：同一段 script 只註冊一次，改了原文自然是新的一筆。
    """
    return get_redis().register_script(lua)

"""SSE 事件緩衝區（09 §3.2 的 resume buffer、1D-4a）。

**產生端與讀取端之間唯一的通道。** 讀 LLM 的那個 task 只管把事件放進來，回應串流只管
把事件取出去——兩邊不互相持有，於是三件事同時成立：

1. **client 斷線不影響生成**（06 §4 的 G-06）：產生端根本不知道有沒有人在讀。
2. **resume 是同一條路**（1D-4b）：重連就是從 `Last-Event-ID` 之後再讀一次。
3. **跨行程**：正式環境是每 replica 兩個 uvicorn worker × N replica（11 §45），重連
   幾乎不會落回原本那個行程；放在行程內的佇列的話，重連只會拿到空的。

**用 Redis Stream 而不是 List**：Stream 同時給得出「阻塞等下一筆」（XREAD BLOCK）與
「從某個編號往後讀」（XRANGE），前者是即時轉發、後者是 resume，兩者共用同一份資料。
用 List 的話，前者要靠輪詢或另外接一個 pub/sub——那是兩份資料，而它們會不一致。

**編號就是 Redis 的 entry id**（`{seq}-0`），不另外存一份。SSE 的 `id:` 欄位、
`Last-Event-ID`、以及這裡的範圍查詢因此是同一個數字；分成兩份的話，兩邊差一的錯誤
會表現成「重連之後少一段字」，而那極難重現。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from config.settings.app_settings import get_app_settings
from core.redis import get_async_redis, tenant_key

__all__ = ["BUFFER_TTL_SECONDS", "StreamBuffer", "StreamEvent", "settled_ttl_seconds"]

# 09 §3.2：resume buffer TTL 5 分鐘。它同時是成本上限——每一條串流都在 Redis 留一份
# 完整回答，而它的用途只有「斷線後幾秒內接回去」。
BUFFER_TTL_SECONDS = 300


def settled_ttl_seconds() -> int:
    """**收尾之後**的緩衝區壽命（二次架構審計 L1）。

    `BUFFER_TTL_SECONDS` 的 5 分鐘是為了「生成還在跑」那段時間——一條長回答不能在
    中途過期。可是終局事件送出之後，緩衝區剩下的用途只有一個：**client 在收到
    `done` 之前就斷線了，重連回來補讀最後那幾個事件**。那件事只會發生在幾秒內。

    於是每一條串流的完整回答都在 Redis 躺滿 5 分鐘，而它 4 分多鐘都沒有讀者。
    `StreamBuffer.drop()` 的 docstring 從 1D-4b 起就寫著這是「200 併發下的記憶體
    差別」，但**它一個呼叫端都沒有**。

    **縮短而不是刪掉**：直接 `drop()` 會把「斷線後回來續傳」變成 409
    `RESUME_EXPIRED`——那是把一個成本問題換成一個使用者看得見的錯誤。
    """
    return get_app_settings().stream_settled_ttl_seconds


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """一個事件。`seq` 是 SSE 的 `id:`，`type` 是 `event:`，`data` 是 payload。"""

    seq: int
    type: str
    data: dict[str, Any]


class StreamBuffer:
    """一則訊息的事件緩衝區。

    **一個 message_id 一個 buffer**，而寫入端只有一個（產生那則訊息的那個 task）。
    `seq` 因此可以由寫入端自己數，不必每次去問 Redis——那是每個 token 一次額外的
    round trip，而 token 是以百計的。

    **代價：同一個 message_id 不能被寫第二輪。** 新的 instance 一律從 1 開始數，而
    Redis 的 entry id 必須嚴格遞增——舊的事件還在（TTL 5 分鐘）的話，第二輪的第一個
    `XADD id=1-0` 會被拒。而**看到的錯誤訊息連原因都不會說**：`append` 走 pipeline，
    redis-py 6.4.0 的 `Pipeline.annotate_exception` 少一個 f 前綴，Redis 原本那句
    「equal or smaller than the target stream top item」會被替換成字面上的
    ``{exception.args}``。目前寫入端唯一（一則訊息生成一次），所以不會發生；
    **未來要加「失敗重跑」的話，重跑前必須先 `drop()`**——由
    tests/integration/test_stream_buffer.py 的 `TestRegeneration` 守著。
    """

    def __init__(self, *, tenant_id: uuid.UUID, message_id: uuid.UUID) -> None:
        self.key = tenant_key(tenant_id, "sse", str(message_id))
        self._seq = 0

    async def append(self, type_: str, data: Mapping[str, Any]) -> StreamEvent:
        """加一個事件。回傳的 `seq` 就是它的 SSE id。"""
        self._seq += 1
        event = StreamEvent(seq=self._seq, type=type_, data=dict(data))
        client = get_async_redis()
        # pipeline：XADD 與 EXPIRE 一次來回。分成兩次的話，每個 token 都要付兩趟。
        async with client.pipeline(transaction=False) as pipe:
            pipe.xadd(
                self.key,
                {"type": event.type, "data": json.dumps(event.data, ensure_ascii=False)},
                id=f"{event.seq}-0",
            )
            # **每次都重設 TTL**，不是只在建立時設一次：一條跑了六分鐘的長回答會在
            # 中途過期，而症狀是「長回答的 resume 一定失敗、短回答都正常」。
            pipe.expire(self.key, BUFFER_TTL_SECONDS)
            await pipe.execute()
        return event

    async def history(self, *, after: int = 0) -> list[StreamEvent]:
        """`after` 之後的全部事件（不含 `after` 本身）。resume 用的就是它。"""
        client = get_async_redis()
        rows = await client.xrange(self.key, min=f"({after}-0", max="+")
        return [_decode(entry_id, fields) for entry_id, fields in rows]

    async def follow(self, *, after: int = 0, block_ms: int = 1000) -> list[StreamEvent]:
        """等下一批事件；`block_ms` 之內沒有就回空清單。

        **逾時回空清單而不是例外**：讀取端要靠這個空回合送心跳（09 §3.2 的 15 秒），
        而「沒有新事件」是串流最正常的狀態——LLM 正在想下一個 token。
        """
        # **唯一用阻塞 client 的地方**（`blocking=True` = 沒有 socket_timeout）：這條
        # 指令本來就要等，逾時由 `block_ms` 決定。其餘指令一律走有逾時的那一個，
        # 否則 Redis 半死不活時它們會無限期掛在 event loop 上（見 core/redis.py）。
        client = get_async_redis(blocking=True)
        result = await client.xread({self.key: f"{after}-0"}, block=block_ms)
        if not result:
            return []
        _, rows = cast("Any", result)[0]
        return [_decode(entry_id, fields) for entry_id, fields in rows]

    async def exists(self) -> bool:
        """緩衝區還在嗎？TTL 過期之後就不在了（09 §3.2 的 5 分鐘）。

        續傳前要問這件事：不在的話，client 要求接續的那些事件已經沒了，正確的回應是
        `409 RESUME_EXPIRED` 而不是一條空的串流——空串流對 client 的意思是「這則訊息
        沒有內容」，而它其實有，只是要改用 `GET /messages` 去抓。
        """
        return bool(await get_async_redis().exists(self.key))

    async def request_stop(self) -> None:
        """標記「這則生成要停下來」（1D-4b 的中止）。

        **旗標放 Redis 而不是行程記憶體**：正式環境是每 replica 兩個 uvicorn worker ×
        N replica（11 §45），中止請求幾乎不會落回產生那則訊息的行程。放在記憶體裡的話，
        它只停得了剛好接到請求的那一台——使用者按了停止，而帳單繼續跑。

        TTL 與緩衝區相同：生成最長也就跑那麼久，留著只會累積垃圾。
        """
        await get_async_redis().set(self._stop_key, "1", ex=BUFFER_TTL_SECONDS)

    async def stop_requested(self) -> bool:
        return bool(await get_async_redis().exists(self._stop_key))

    @property
    def _stop_key(self) -> str:
        return f"{self.key}:stop"

    async def ttl_seconds(self) -> int:
        return int(await get_async_redis().ttl(self.key))

    async def settle(self) -> None:
        """終局事件送出之後，把緩衝區的壽命縮到 `settled_ttl_seconds()`。

        **一定要在最後一個 `append()` 之後呼叫**：`append` 每次都把 TTL 重設回
        `BUFFER_TTL_SECONDS`（那是它該做的——長回答不能中途過期），所以順序反過來
        就等於沒做。呼叫端是 `ChatService._complete` / `_fail`，兩者都在送出
        `done` / `error` 之後。

        **stop 旗標一起縮**：它與緩衝區同生共死，留著一個沒有緩衝區的旗標只會讓
        下一次的 `stop_requested()` 多一次 round trip。
        """
        ttl = settled_ttl_seconds()
        client = get_async_redis()
        async with client.pipeline(transaction=False) as pipe:
            pipe.expire(self.key, ttl)
            pipe.expire(self._stop_key, ttl)
            await pipe.execute()

    async def drop(self) -> None:
        """整個刪掉。

        **正常收尾走的是 `settle()` 不是這裡**：刪掉會讓「斷線後回來續傳」變成 409。
        這支留著是給「失敗重跑」用的——重跑前必須先刪，否則 entry id 不遞增（見本
        類別 docstring），由 tests/integration/test_stream_buffer.py 的
        `TestRegeneration` 守著。
        """
        await get_async_redis().delete(self.key)


def _decode(entry_id: str, fields: Mapping[str, str]) -> StreamEvent:
    """Redis entry → `StreamEvent`。id 的形狀是 `{seq}-0`（見模組 docstring）。"""
    return StreamEvent(
        seq=int(entry_id.split("-", 1)[0]),
        type=fields["type"],
        data=json.loads(fields["data"]),
    )

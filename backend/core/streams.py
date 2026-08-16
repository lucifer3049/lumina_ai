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

from core.redis import get_async_redis, tenant_key

__all__ = ["BUFFER_TTL_SECONDS", "StreamBuffer", "StreamEvent"]

# 09 §3.2：resume buffer TTL 5 分鐘。它同時是成本上限——每一條串流都在 Redis 留一份
# 完整回答，而它的用途只有「斷線後幾秒內接回去」。
BUFFER_TTL_SECONDS = 300


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
        client = get_async_redis()
        result = await client.xread({self.key: f"{after}-0"}, block=block_ms)
        if not result:
            return []
        _, rows = cast("Any", result)[0]
        return [_decode(entry_id, fields) for entry_id, fields in rows]

    async def ttl_seconds(self) -> int:
        return int(await get_async_redis().ttl(self.key))

    async def drop(self) -> None:
        """正常結束後主動清掉，不必等 TTL——那是 200 併發下的記憶體差別。"""
        await get_async_redis().delete(self.key)


def _decode(entry_id: str, fields: Mapping[str, str]) -> StreamEvent:
    """Redis entry → `StreamEvent`。id 的形狀是 `{seq}-0`（見模組 docstring）。"""
    return StreamEvent(
        seq=int(entry_id.split("-", 1)[0]),
        type=fields["type"],
        data=json.loads(fields["data"]),
    )

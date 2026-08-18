"""驗收：SSE 事件緩衝區（09 §3.2 的 resume buffer、13 §3 工作包 1D-4a）。

**這個緩衝區同時是三件事的機制**，而那是它值得獨立存在的理由：

1. **傳輸匯流排**：產生端（讀 LLM 的那個 task）與讀取端（回應串流）之間唯一的通道。
   兩邊解耦之後，client 斷線不會影響產生端繼續收完（06 §4 的 G-06，1D-4b）。
2. **resume 的來源**：重連時從 `Last-Event-ID` 之後讀同一個結構（1D-4b）。
3. **跨行程**：正式環境是每個 replica 兩個 uvicorn worker × N replica（11 §45），
   重連幾乎不會落回原來那個行程。放在行程內的佇列的話，重連只會拿到空的。

因此它落在 Redis（11 §157 明列「SSE resume buffer 在 Redis」），而且用 **Stream**
而不是 List：Stream 同時給得出「阻塞等下一筆」與「從某個編號往後讀」，前者是即時
轉發、後者是 resume，兩者共用同一份資料。

**本檔只驗這個原語**。端點怎麼用它在 `tests/api/test_chat_stream.py`。
"""

from __future__ import annotations

import uuid

import pytest

from core.streams import BUFFER_TTL_SECONDS, StreamBuffer
from tests.conftest import TENANT_A, TENANT_B

pytestmark = pytest.mark.django_db  # Redis 而非 DB，但沿用測試的租戶常數與清理


@pytest.fixture
def buffer() -> StreamBuffer:
    return StreamBuffer(tenant_id=TENANT_A, message_id=uuid.uuid4())


class TestKeyNaming:
    def test_the_key_is_tenant_prefixed(self, buffer: StreamBuffer) -> None:
        """鐵則 4：Redis key 一律 `t:{tenant_id}:` 前綴。

        少了它，兩個租戶的串流會共用同一個 key——而 message_id 是 UUID，實際上不會
        相撞，所以這個錯誤永遠不會有症狀，直到有人用可預測的 id 為止。
        """
        assert buffer.key.startswith(f"t:{TENANT_A}:")

    def test_different_tenants_never_share_a_key(self) -> None:
        message_id = uuid.uuid4()

        a = StreamBuffer(tenant_id=TENANT_A, message_id=message_id)
        b = StreamBuffer(tenant_id=TENANT_B, message_id=message_id)

        assert a.key != b.key


class TestAppendAndRead:
    async def test_events_come_back_in_order(self, buffer: StreamBuffer) -> None:
        """順序就是語意：delta 的順序是句子的順序。"""
        for index in range(5):
            await buffer.append("delta", {"text": str(index)})

        events = await buffer.history()

        assert [event.data["text"] for event in events] == ["0", "1", "2", "3", "4"]

    async def test_sequence_numbers_start_at_one_and_increase(self, buffer: StreamBuffer) -> None:
        """`seq` 就是 SSE 的 event id，而 `Last-Event-ID` 的語意是「我收到這個號碼了」。

        從 0 開始的話，「還沒收到任何東西」與「收到第 0 號」在 client 那邊分不出來。
        """
        await buffer.append("meta", {})
        await buffer.append("delta", {"text": "a"})

        events = await buffer.history()

        assert [event.seq for event in events] == [1, 2]

    async def test_reading_after_a_sequence_skips_what_was_delivered(
        self, buffer: StreamBuffer
    ) -> None:
        """resume 的原語（1D-4b 會用它接 `Last-Event-ID`）：不重、不漏。

        重的話使用者會看到重複的字；漏的話會少一段——兩者都比整段重來更難察覺。
        """
        for index in range(4):
            await buffer.append("delta", {"text": str(index)})

        events = await buffer.history(after=2)

        assert [event.seq for event in events] == [3, 4]

    async def test_reading_past_the_end_returns_nothing(self, buffer: StreamBuffer) -> None:
        await buffer.append("delta", {"text": "a"})

        assert await buffer.history(after=99) == []

    async def test_payloads_survive_a_round_trip(self, buffer: StreamBuffer) -> None:
        """中文、換行、巢狀結構都要原樣回來——citations（1D-5）是一個物件陣列。"""
        payload = {"text": "第一段\n第二段", "items": [{"chunk_id": "c-1", "score": 0.87}]}

        await buffer.append("delta", payload)

        assert (await buffer.history())[0].data == payload

    async def test_an_empty_buffer_reads_as_empty(self, buffer: StreamBuffer) -> None:
        """還沒開始產生時讀到的是空的，**不是錯誤**：讀取端會先到，因為產生端要先
        呼叫 LLM。"""
        assert await buffer.history() == []


class TestFollow:
    async def test_it_waits_for_the_next_event(self, buffer: StreamBuffer) -> None:
        """讀取端要能「等下一筆」而不是輪詢——輪詢在 200 併發串流下是純粹的浪費，
        而間隔拉長就會直接變成使用者看到的延遲。"""
        await buffer.append("delta", {"text": "先來的"})

        events = await buffer.follow(after=0, block_ms=50)

        assert [event.data["text"] for event in events] == ["先來的"]

    async def test_it_returns_empty_when_nothing_arrives(self, buffer: StreamBuffer) -> None:
        """等待逾時回空清單而**不是**例外：讀取端要靠這個空回合送心跳（09 §3.2）。"""
        assert await buffer.follow(after=0, block_ms=50) == []

    async def test_following_does_not_replay_delivered_events(self, buffer: StreamBuffer) -> None:
        await buffer.append("delta", {"text": "a"})
        await buffer.append("delta", {"text": "b"})

        first = await buffer.follow(after=0, block_ms=50)
        second = await buffer.follow(after=first[-1].seq, block_ms=50)

        assert [event.data["text"] for event in first] == ["a", "b"]
        assert second == []


class TestExpiry:
    def test_the_ttl_matches_the_spec(self) -> None:
        """09 §3.2：resume buffer TTL 5 分鐘。

        它同時是**成本上限**：每一條串流都在 Redis 留一份完整回答，沒有 TTL 的話那
        份資料會永久累積，而它的用途只有「斷線後幾秒內接回去」。
        """
        assert BUFFER_TTL_SECONDS == 300

    async def test_the_ttl_is_actually_applied(self, buffer: StreamBuffer) -> None:
        """設在**每次 append**：只在建立時設的話，一條跑了六分鐘的長回答會在中途過期，
        而症狀是「長回答的 resume 一定失敗、短回答都正常」。
        """
        await buffer.append("delta", {"text": "a"})

        assert 0 < await buffer.ttl_seconds() <= BUFFER_TTL_SECONDS


class TestCleanup:
    async def test_a_buffer_can_be_dropped(self, buffer: StreamBuffer) -> None:
        """正常結束後可以主動清掉，不必等 TTL——那是 200 併發下的記憶體差別。"""
        await buffer.append("done", {})

        await buffer.drop()

        assert await buffer.history() == []

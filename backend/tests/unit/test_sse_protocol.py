"""驗收：SSE 的線上格式（09 §3.2、13 §3 工作包 1D-4a）。

**SSE 是一個用換行斷句的協定**，而我們要送的內容裡就有換行——LLM 的回答有段落、有
程式碼區塊、有清單。這個組合是整個協定唯一真正危險的地方：一個沒有跳脫的換行不會
報錯，只會讓瀏覽器把半句話當成一個事件的結束，而後面的內容變成一個不存在的事件名稱。
症狀是「偶爾少一段字」，且只在回答裡剛好有換行時發生。

本檔只驗**格式化**這一層（純函式，不碰網路與 DB）：事件怎麼變成位元組、心跳長什麼樣、
哪些欄位進 payload。端點層的行為在 `tests/api/test_chat_stream.py`。

三件事錯了都不會有例外：

1. **換行沒跳脫** —— 見上。
2. **心跳帶了 id** —— `Last-Event-ID` 會被心跳推進，於是重連時 client 說「我收到第 N
   號了」，而第 N 號其實是一個沒有內容的心跳，中間真正的事件就被跳過去了（1D-4b）。
3. **事件名稱寫錯** —— `EventSource` 依名稱分派 listener，名字錯的事件會安靜地掉進
   預設的 `message` handler，前端的狀態機完全收不到。
"""

from __future__ import annotations

import json

import pytest

from api.sse import HEARTBEAT_SECONDS, format_event, format_heartbeat
from core.streams import StreamEvent


def _event(seq: int = 1, type_: str = "delta", **data: object) -> StreamEvent:
    return StreamEvent(seq=seq, type=type_, data=dict(data))


def _lines(payload: str) -> list[str]:
    return payload.split("\n")


class TestWireFormat:
    def test_an_event_has_id_event_and_data_lines(self) -> None:
        text = format_event(_event(seq=7, type_="delta", text="嗨"))

        assert _lines(text)[0] == "id: 7"
        assert _lines(text)[1] == "event: delta"
        assert _lines(text)[2].startswith("data: ")

    def test_an_event_ends_with_a_blank_line(self) -> None:
        """**空行是事件的結束符號。** 少了它，client 會一直等下一行，而畫面上是
        「送出之後什麼都沒發生」——沒有錯誤、沒有逾時，就只是不動。"""
        text = format_event(_event())

        assert text.endswith("\n\n")

    def test_the_payload_is_json(self) -> None:
        text = format_event(_event(text="年假 14 天"))

        data = json.loads(_lines(text)[2].removeprefix("data: "))
        assert data == {"text": "年假 14 天"}

    def test_a_newline_inside_the_payload_never_breaks_the_frame(self) -> None:
        """**本檔最重要的一條。** 回答裡的換行必須以 `\\n` 進 JSON，而不是真的換行。

        真的換行的話，那一行之後的內容會被 client 當成新的一行欄位——`程式碼` 這種
        開頭會變成一個不認得的欄位名而被丟掉，使用者看到的是「回答少了一段」。
        """
        text = format_event(_event(text="第一段\n\n第二段"))

        assert len([line for line in _lines(text) if line.startswith("data: ")]) == 1
        assert "\\n" in text

    def test_the_payload_keeps_chinese_readable(self) -> None:
        """不做 ASCII 轉義：`\\u5e74\\u5047` 這種 payload 在瀏覽器 devtools 與 log 裡
        完全不可讀，而這條路徑的除錯幾乎都是在那兩個地方進行的。"""
        text = format_event(_event(text="年假"))

        assert "年假" in text

    def test_carriage_returns_are_escaped_too(self) -> None:
        """Windows 來源的文件會帶 `\\r`。SSE 的行結束符是 `\\n`，但 `\\r` 會被部分
        client 一併吃掉，導致同一段內容在不同瀏覽器上長度不同。"""
        text = format_event(_event(text="a\r\nb"))

        assert "\r" not in text.replace("\\r", "")


class TestHeartbeat:
    def test_it_is_a_comment(self) -> None:
        """`:` 開頭是 SSE 的註解行，client 收到但不觸發任何 listener。它的用途是讓
        中間的 proxy 看到流量——閒置的連線常在 30~60 秒被掐掉（12 §56）。"""
        assert format_heartbeat().startswith(":")

    def test_it_carries_no_id(self) -> None:
        """帶 id 的話 `Last-Event-ID` 會被心跳推進，而重連時 client 報的那個號碼指向
        一個沒有內容的心跳——中間真正的事件會被跳過（1D-4b 的 resume 直接壞掉）。"""
        assert "id:" not in format_heartbeat()

    def test_it_ends_the_frame(self) -> None:
        assert format_heartbeat().endswith("\n\n")

    def test_the_interval_matches_the_spec(self) -> None:
        """09 §3.2：每 15 秒。太長會被 proxy 掐掉，太短是純粹的流量浪費——
        200 併發串流下，每一秒的心跳都乘以 200。"""
        assert HEARTBEAT_SECONDS == 15


class TestEventNames:
    """09 §3.2 的七種事件。**名稱是 API 契約**：前端依名稱掛 listener。"""

    @pytest.mark.parametrize(
        "name", ["meta", "delta", "tool_call", "citations", "usage", "done", "error"]
    )
    def test_every_documented_event_name_is_accepted(self, name: str) -> None:
        text = format_event(_event(type_=name))

        assert f"event: {name}" in text

    def test_meta_carries_what_the_client_needs_to_render_the_bubble(self) -> None:
        """`message_id` 是後續 stop／regenerate 的定位鍵，也是重連時要抓的最終訊息。
        少了它，前端只能等 done——而中斷的串流永遠不會有 done。"""
        text = format_event(
            _event(type_="meta", message_id="m-1", model="mock-chat", conversation_id="c-1")
        )

        data = json.loads(_lines(text)[2].removeprefix("data: "))
        assert set(data) == {"message_id", "model", "conversation_id"}

    def test_usage_carries_the_billing_numbers(self) -> None:
        text = format_event(_event(type_="usage", prompt_tokens=10, completion_tokens=5, cost=0.0))

        data = json.loads(_lines(text)[2].removeprefix("data: "))
        assert {"prompt_tokens", "completion_tokens", "cost"} <= set(data)

    def test_error_carries_a_stable_code(self) -> None:
        """09 §1.3：client 只該依 `code` 分支，不該解析訊息。`retryable` 決定前端要
        顯示「重試」還是「這題不用再試了」。"""
        text = format_event(
            _event(type_="error", code="STREAM_INTERRUPTED", title="生成中斷", retryable=True)
        )

        data = json.loads(_lines(text)[2].removeprefix("data: "))
        assert data["code"] == "STREAM_INTERRUPTED"
        assert data["retryable"] is True

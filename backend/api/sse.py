"""SSE 的線上格式（09 §3.2、1D-4a）。

**SSE 是一個用換行斷句的協定**，而我們要送的內容裡就有換行——LLM 的回答有段落、有
程式碼區塊、有清單。這個組合是整個協定唯一真正危險的地方：沒跳脫的換行不會報錯，
只會讓 client 把半句話當成事件結束，後面的內容變成一個不存在的欄位名而被丟掉。
症狀是「偶爾少一段字」，且只在回答裡剛好有換行時發生。

因此 payload 一律是**一行 JSON**：`json.dumps` 會把 `\\n` 與 `\\r` 轉成跳脫序列，
而那正好讓「內容裡的換行」與「協定的換行」不再是同一個東西。

這一層放在 `api/` 而不是 `core/`：它是**線上格式**，只有 HTTP 這個出口需要它。
產生端寫進緩衝區的是與傳輸無關的 `StreamEvent`（core/streams.py），1D-4b 的 resume
與未來若有別的傳輸（WebSocket）都讀同一份資料。
"""

from __future__ import annotations

import json

from core.streams import StreamEvent

__all__ = ["HEARTBEAT_SECONDS", "format_event", "format_heartbeat"]

# 09 §3.2：每 15 秒一個心跳。閒置的連線常被中間的 proxy 在 30~60 秒掐掉（12 §56），
# 而太短是純粹的浪費——200 併發串流下，每一次心跳都乘以 200。
HEARTBEAT_SECONDS = 15


def format_event(event: StreamEvent) -> str:
    """`StreamEvent` → 線上的位元組。

    `id:` 是 `Last-Event-ID` 的來源（1D-4b 的 resume）；`event:` 決定 client 分派到
    哪一個 listener，名字寫錯的事件會安靜地掉進預設的 handler。
    """
    payload = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"


def format_heartbeat() -> str:
    """`:` 開頭是 SSE 的註解行——client 收得到，但不會觸發任何 listener。

    **刻意不帶 id**：帶了的話 `Last-Event-ID` 會被心跳推進，而重連時 client 報的那個
    號碼指向一個沒有內容的心跳，中間真正的事件就被跳過去了。
    """
    return ": heartbeat\n\n"

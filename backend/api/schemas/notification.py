"""`/notifications` 的 I/O 契約（09 §2.6，2A-5）。

分頁形狀是全站慣例的 `{items, next_cursor}`（09 §1.1），**多一個 `unread_count`**
（05／09 待同步）：鈴鐺上的數字與清單必須出自同一次查詢——分成兩個請求時，兩者
之間進來的新通知會讓數字與內容對不起來，而使用者只會覺得這個系統怪怪的。

`meta` 是自由形狀的 dict：欄位隨事件型別而異（文件事件是 document_id／stage，
quota 是 resource／threshold），寫死成具名模型會讓每加一種事件就要改契約。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class NotificationOut(BaseModel):
    """一則通知。欄位對映 05 §3.3 的 notifications。"""

    id: uuid.UUID
    type: str
    title: str
    body: str
    channels: list[str]
    meta: dict[str, Any]
    read_at: datetime | None
    created_at: datetime
    # 收合（同一批文件完成）會讓它前進，`created_at` 不動——收件匣依它排序。
    updated_at: datetime


class NotificationListOut(BaseModel):
    items: list[NotificationOut]
    next_cursor: str | None
    unread_count: int

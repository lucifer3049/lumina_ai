"""`/audit-logs` 的 I/O 契約（09 §2.6，2A-4）。

分頁形狀是全站慣例的 `{items, next_cursor}`（09 §1.1）；`next_cursor` 為 null
代表沒有下一頁。

`before`／`after` 是自由形狀的 dict：欄位隨資源而異（KB 是 name、使用者是
display_name／is_active），寫死成具名模型會讓每加一種被稽核的資源就要改契約。
內容由 service 端的白名單決定，不是把整個物件倒出來（10 §5）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditLogOut(BaseModel):
    """一列稽核。欄位對映 05 §3.3 的 audit_logs。"""

    id: uuid.UUID
    action: str
    actor_id: uuid.UUID | None
    actor_type: str
    resource_type: str
    resource_id: uuid.UUID | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    # succeeded / denied / failed
    outcome: str
    status: int | None
    # 被拒的 permission code（10 §3）；只有 outcome=denied 有值。
    permission: str | None
    ip: str | None
    user_agent: str
    request_id: str
    created_at: datetime


class AuditLogListOut(BaseModel):
    items: list[AuditLogOut]
    next_cursor: str | None

"""`/tenants/current` 的 I/O 契約（09 §2.2）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    plan: str
    status: str


class TenantUpdateIn(BaseModel):
    """本階段只開放改名稱。

    ``slug`` 刻意不可改：它是登入的識別字，改掉等於所有既有的登入連結失效，
    而且 1A 沒有處理「舊 slug 導向新 slug」的機制。``plan`` 屬計費，2A 才有。
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)


class QuotaItemOut(BaseModel):
    """一種資源的即時額度（2A-2a）。`limit`／`remaining` 為 null＝不限制，
    `resets_at` 為 null＝存量或 gauge（沒有週期）。"""

    resource: str
    limit: int | None
    used: int
    remaining: int | None
    resets_at: datetime | None


class QuotaOut(BaseModel):
    items: list[QuotaItemOut]

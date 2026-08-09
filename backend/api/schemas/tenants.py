"""`/tenants/current` 的 I/O 契約（09 §2.2）。"""

from __future__ import annotations

import uuid

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

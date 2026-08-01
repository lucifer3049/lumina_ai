"""Spike 端點的 Response DTO（02 §api/schemas：按 context 分檔）。

**為什麼需要這個檔案**：原本 repository 的 ``values()`` 裸 dict 一路傳到
endpoint，OpenAPI 產出的是無型別的 generic object——鐵則 10 要求前端吃
``pnpm gen:api`` 的 codegen 結果，那種 schema 產不出可用的型別。

欄位與 ``repositories.spike.SpikeItemRow`` 對齊；兩邊不同步時 mypy 會在
endpoint 的組裝處擋下來。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SpikeItemOut(BaseModel):
    """單筆 spike item —— 壓測目標端點的回應元素。"""

    id: int
    title: str
    created_at: datetime

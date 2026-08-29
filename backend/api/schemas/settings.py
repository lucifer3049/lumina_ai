"""`/settings` 的 I/O 契約（09 §2.6，2C-1）。

**形狀刻意留成自由的物件**，理由同 2B-5 的 KB `config`：逐鍵宣告成 pydantic 欄位的
話，這裡會變成參數清單的第二份，而它與 `services/knowledge/kb_config.py` 的那一份漂
掉時沒有任何測試會紅——症狀是新加的參數在 OpenAPI 上看不見，前端因此送不出去。
驗證一律在寫入端做（`TenantSettingsService`），逐欄位回報。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TenantSettingsOut(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)


class TenantSettingsUpdateIn(BaseModel):
    """``None`` = 這次沒送（不是「清空」）。

    清空是逐區的明確 ``{}``（`{"retrieval": {}}`），與「這次沒動這一區」分得開——
    整份取代的話，畫面上存一個分頁會清掉另一個分頁，而 API 回 200。
    """

    settings: dict[str, Any] | None = None

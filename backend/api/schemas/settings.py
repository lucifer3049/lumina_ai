"""`/settings` 的 I/O 契約（09 §2.6，2C-1）。

**形狀刻意留成自由的物件**，理由同 2B-5 的 KB `config`：逐鍵宣告成 pydantic 欄位的
話，這裡會變成參數清單的第二份，而它與 `services/knowledge/kb_config.py` 的那一份漂
掉時沒有任何測試會紅——症狀是新加的參數在 OpenAPI 上看不見，前端因此送不出去。
驗證一律在寫入端做（`TenantSettingsService`），逐欄位回報。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CredentialOut(BaseModel):
    """憑證的**遮罩**（09 §2.6「唯寫不回讀明文」，2C-2）。

    這裡刻意只有三個欄位，而且沒有一個放得下金鑰：名字（哪一把）、末四碼（是不是我
    以為的那一把）、更新時間（上次換是什麼時候）。**存在本身就是「已設定」**——
    另開一個 `configured` 布林欄位的話，它與「這一列在不在」遲早會不一致。
    """

    name: str
    # 末四碼。前四碼會洩漏種類與環境（`sk-live-`），見 `Credential` 的 docstring。
    hint: str
    updated_at: datetime


class TenantSettingsOut(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)
    # **與 `settings` 分開的欄位**：憑證不住在 `tenant.settings` 裡（那一欄會整份
    # 回給前端、也會被參數解析讀去），分開才使「settings 裡永遠沒有秘密」成為一條
    # 看得見的規則，而不是一句註解。
    credentials: list[CredentialOut] = Field(default_factory=list)


class TenantSettingsUpdateIn(BaseModel):
    """``None`` = 這次沒送（不是「清空」）。

    清空是逐區的明確 ``{}``（`{"retrieval": {}}`），與「這次沒動這一區」分得開——
    整份取代的話，畫面上存一個分頁會清掉另一個分頁，而 API 回 200。
    """

    settings: dict[str, Any] | None = None

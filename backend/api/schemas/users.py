"""`/users` 的 I/O 契約（09 §2.2）。

輸出型別是**明確列欄位**的，不是從 model 自動產生：自動產生的話，任何人在
model 上加一個敏感欄位（下一個就是 `password_hash`）都會自動流到 client，
而那不會有任何測試紅燈。
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    status: str
    roles: list[str]


class UserListOut(BaseModel):
    """包一層 ``items`` 而不是直接回陣列。

    裸陣列的回應之後要加分頁資訊（total、cursor）時就是破壞性變更；包一層之後
    加欄位是相容的。09 §1 的分頁契約會在 1B 有大量資料的端點時落地。
    """

    items: list[UserOut]


class UserCreateIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=8, max_length=1024)
    # 不給就沒有角色：**沒有角色 = 沒有任何權限**（PermissionService 的預設）。
    # 這比「預設給 viewer」安全——預設值會讓「忘了指派」變成看不見的授權。
    role: str | None = None


class UserUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=8, max_length=1024)

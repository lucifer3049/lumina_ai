"""`/auth` 的 I/O 契約（09 §2.1）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    """登入請求。

    ``tenant_slug`` 是必填的，因為登入發生在租戶身分存在**之前**：RLS 之下，
    沒有租戶就查不到任何使用者，而 email 只在租戶內唯一（05 §3.1）。
    slug → tenant_id 由不含 RLS 的目錄表解析（apps/identity/models.py 的
    `TenantDirectory` 有完整說明）。

    未來改成子網域登入時，這個欄位改由 Host 標頭推導，契約可維持相容。
    """

    tenant_slug: str = Field(min_length=1, max_length=63)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class TokenPairOut(BaseModel):
    """**只有 access token**。refresh 走 httpOnly cookie，不進 body。

    放進 body 的話前端就得自己存，而 localStorage 是 XSS 的直接目標；httpOnly
    讓 JavaScript 完全讀不到它。
    """

    access_token: str
    # 下一行的 noqa: S105 —— "Bearer" 是 RFC 6750 的型別名稱，不是密碼。
    token_type: str = "Bearer"  # noqa: S105
    expires_in: int

"""`/auth` 端點（09 §2.1）。

鐵則 3（Controller 三行原則）：解析請求 → 呼叫一個 Service 方法 → 回傳。
登入的鎖定、雜湊比對、rotation 狀態機全部在 `services/identity/auth.py`。

**cookie 的組裝留在這一層**：它是 HTTP 傳輸細節（response shaping），不是業務
規則。反過來讓 service 回一個 Response 會要求 services/ 認識 FastAPI，方向錯了。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Response, status

from api.dependencies.auth import Principal, require_authenticated
from api.schemas.auth import LoginIn, TokenPairOut
from api.schemas.problem import ERROR_RESPONSES
from api.schemas.users import PasswordChangeIn
from config.settings.app_settings import get_app_settings
from core.db import run_orm
from services.identity.auth import AuthService, TokenPair
from services.identity.tokens import REFRESH_TTL
from services.identity.users import UserService

router = APIRouter(prefix="/auth", tags=["auth"], responses=ERROR_RESPONSES)
_service = AuthService()
_users = UserService()

REFRESH_COOKIE = "refresh_token"


def _set_refresh_cookie(response: Response, pair: TokenPair) -> None:
    settings = get_app_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        pair.refresh_token,
        max_age=int(REFRESH_TTL.total_seconds()),
        httponly=True,
        # Secure 只在本機開發關掉，理由見 AppSettings.refresh_cookie_secure。
        secure=settings.refresh_cookie_secure,
        # Lax 而非 Strict：Strict 會讓「從 email 連結點進來」的第一個請求不帶
        # cookie，使用者看到的是被登出。Lax 擋得住跨站 POST（CSRF 的主要形狀），
        # 同時保留正常的站外導流。
        samesite="lax",
        path="/api/v1/auth",
    )


@router.post("/login", operation_id="auth_login")
async def login(payload: LoginIn, response: Response) -> TokenPairOut:
    tenant_id = await run_orm(_service.resolve_tenant, payload.tenant_slug)
    pair = await run_orm(
        _service.login,
        tenant_id=tenant_id,
        email=payload.email,
        password=payload.password,
    )
    _set_refresh_cookie(response, pair)
    return _as_out(pair)


@router.post("/refresh", operation_id="auth_refresh")
async def refresh(
    response: Response,
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> TokenPairOut:
    pair = await run_orm(_service.refresh, refresh_token or "")
    _set_refresh_cookie(response, pair)
    return _as_out(pair)


@router.post("/logout", operation_id="auth_logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    principal: Annotated[Principal, Depends(require_authenticated)],
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> None:
    await run_orm(
        _service.logout,
        tenant_id=principal.tenant_id,
        jti=principal.jti,
        access_expires_at=principal.access_expires_at,
        refresh_token=refresh_token,
    )
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")


@router.post(
    "/password/change",
    operation_id="auth_change_password",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def change_password(
    payload: PasswordChangeIn,
    principal: Annotated[Principal, Depends(require_authenticated)],
    response: Response,
) -> None:
    """改密碼並撤銷**全部**session（10 §2.1）。

    改密碼的典型情境就是「我懷疑帳號被盜」——只更新雜湊而不撤銷的話，攻擊者
    手上的 token 完全不受影響，而使用者以為自己已經處理完了。
    """
    await run_orm(
        _users.change_password,
        principal.tenant_id,
        principal.user_id,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    # 自己這台裝置也一起登出：refresh 家族已隨撤銷失效，留著 cookie 只會讓
    # 下一次 refresh 得到一個看起來莫名其妙的 401。
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")


def _as_out(pair: TokenPair) -> TokenPairOut:
    from datetime import UTC, datetime

    return TokenPairOut(
        access_token=pair.access_token,
        expires_in=int((pair.expires_at - datetime.now(UTC)).total_seconds()),
    )

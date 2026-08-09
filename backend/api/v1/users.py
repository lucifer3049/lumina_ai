"""`/users` 端點（09 §2.2）。

鐵則 3（Controller 三行原則）：解析請求 → 呼叫一個 Service 方法 → 回傳。

**權限宣告寫在每個端點上**（10 §3）：讀這個檔案就知道每條路需要什麼權限，
而且可以自動彙整成權限矩陣文件。`/users/me` 刻意不掛任何權限碼——每個人都該
看得到、改得了自己的資料，把它綁在 ``user:write`` 之下的後果是「一般員工連自己
的顯示名稱都不能改」，而補上那個權限之後他就同時能建立與停用別人的帳號。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from api.dependencies.auth import Principal, require_authenticated
from api.dependencies.permissions import RequireScope
from api.schemas.problem import ERROR_RESPONSES
from api.schemas.users import UserCreateIn, UserListOut, UserOut, UserUpdateIn
from core.db import run_orm
from services.identity.users import UserService, UserView

router = APIRouter(prefix="/users", tags=["users"], responses=ERROR_RESPONSES)
_service = UserService()


def _out(view: UserView) -> UserOut:
    return UserOut(
        id=view.id,
        email=view.email,
        display_name=view.display_name,
        status=view.status,
        roles=list(view.roles),
    )


# 順序有意義：``/users/me`` 必須排在 ``/users/{user_id}`` 之前，否則 "me" 會被
# 當成 uuid 去比對而回 422。
@router.get("/me", operation_id="users_get_me")
async def get_me(
    principal: Annotated[Principal, Depends(require_authenticated)],
) -> UserOut:
    return _out(await run_orm(_service.get_user, principal.tenant_id, principal.user_id))


@router.patch("/me", operation_id="users_update_me")
async def update_me(
    payload: UserUpdateIn,
    principal: Annotated[Principal, Depends(require_authenticated)],
) -> UserOut:
    view = await run_orm(
        _service.update_profile,
        principal.tenant_id,
        principal.user_id,
        display_name=payload.display_name,
    )
    return _out(view)


@router.get("", operation_id="users_list")
async def list_users(
    principal: Annotated[Principal, Depends(RequireScope("user:read"))],
) -> UserListOut:
    views = await run_orm(_service.list_users, principal.tenant_id)
    return UserListOut(items=[_out(view) for view in views])


@router.post("", operation_id="users_create", status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateIn,
    principal: Annotated[Principal, Depends(RequireScope("user:write"))],
) -> UserOut:
    view = await run_orm(
        _service.create_user,
        principal.tenant_id,
        email=payload.email,
        display_name=payload.display_name,
        password=payload.password,
        role=payload.role,
    )
    return _out(view)


@router.get("/{user_id}", operation_id="users_get")
async def get_user(
    user_id: uuid.UUID,
    principal: Annotated[Principal, Depends(RequireScope("user:read"))],
) -> UserOut:
    return _out(await run_orm(_service.get_user, principal.tenant_id, user_id))


@router.patch("/{user_id}", operation_id="users_update")
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateIn,
    principal: Annotated[Principal, Depends(RequireScope("user:write"))],
) -> UserOut:
    view = await run_orm(
        _service.update_profile,
        principal.tenant_id,
        user_id,
        display_name=payload.display_name,
    )
    return _out(view)


@router.post(
    "/{user_id}/deactivate",
    operation_id="users_deactivate",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def deactivate_user(
    user_id: uuid.UUID,
    principal: Annotated[Principal, Depends(RequireScope("user:write"))],
) -> None:
    await run_orm(_service.deactivate, principal.tenant_id, user_id)

"""`/notifications` 端點（09 §2.6，2A-5）。

Controller 三行原則：解析請求 → 呼叫一個 service 方法 → 回傳。

**權限是「登入者」而不是新的 permission code**（09 §2.6）：收件匣裡的東西本來就
只寄給一個人，因此這裡的授權形狀是**擁有者判定**（service 以 `user_id` 過濾），
不是角色判定。這也是它與 `/audit-logs`、`/analytics/*` 的分野——那兩個是管理面。

沒有「刪除通知」與「全部標為已讀」：前者的價值等同已讀（而通知是到期整批清理的
東西），後者一個迴圈就能做而且會讓「未讀」變成一個沒人相信的數字。要加的話那是
產品決定，不是順手補的端點。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from api.dependencies.auth import Principal, require_authenticated
from api.schemas.notification import NotificationListOut, NotificationOut
from api.schemas.problem import ERROR_RESPONSES
from core.db import run_orm
from services.platform.notifications import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, NotificationService

router = APIRouter(prefix="/notifications", tags=["notifications"], responses=ERROR_RESPONSES)
_service = NotificationService()


@router.get("", operation_id="notifications_list")
async def list_notifications(
    principal: Annotated[Principal, Depends(require_authenticated)],
    unread_only: Annotated[bool, Query(description="只回未讀")] = False,
    cursor: Annotated[str | None, Query(description="上一頁回傳的 next_cursor（不透明）")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> NotificationListOut:
    page = await run_orm(
        _service.inbox,
        principal.tenant_id,
        principal.user_id,
        limit=limit,
        cursor=cursor,
        unread_only=unread_only,
    )
    return NotificationListOut(
        items=[NotificationOut(**asdict(record)) for record in page.items],
        next_cursor=page.next_cursor,
        unread_count=page.unread_count,
    )


@router.patch("/{notification_id}/read", operation_id="notifications_mark_read")
async def mark_notification_read(
    notification_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_authenticated)],
) -> NotificationOut:
    """標為已讀（重複呼叫不改時間，也不 409——重複點擊與多開分頁很常見）。"""
    record = await run_orm(
        _service.mark_read, principal.tenant_id, principal.user_id, notification_id
    )
    return NotificationOut(**asdict(record))

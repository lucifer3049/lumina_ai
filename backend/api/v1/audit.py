"""`/audit-logs` 端點（09 §2.6，2A-4）。

Controller 三行原則：解析參數 → 一個 service 呼叫 → 回傳。

**只有查詢**（04 §8.3 的 Interface 就只有 `query`）：沒有寫入端點、沒有刪除端點、
也沒有「更正」端點。這不是還沒做，是稽核之所以是稽核的原因——寫入口在
middleware 與 service，資料庫上還有 append-only 的 trigger 兜底。

`from`／`to` 是查詢參數的原名（09 §2.6），但 `from` 是 Python 保留字——用 alias
對映，同 `/analytics`。**不給預設範圍**（與 analytics 不同）：稽核的典型用法是
「查這個資源的全部歷史」，預設近 30 天會安靜地切掉更早的那一半。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from api.dependencies.auth import Principal
from api.dependencies.permissions import RequireScope
from api.schemas.audit import AuditLogListOut, AuditLogOut
from api.schemas.problem import ERROR_RESPONSES
from core.db import run_orm
from services.platform.audit import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, AuditService

router = APIRouter(prefix="/audit-logs", tags=["audit"], responses=ERROR_RESPONSES)
_service = AuditService()


@router.get("", operation_id="audit_logs_list")
async def list_audit_logs(
    principal: Annotated[Principal, Depends(RequireScope("audit:read"))],
    action: Annotated[str | None, Query()] = None,
    resource_type: Annotated[str | None, Query()] = None,
    resource_id: Annotated[uuid.UUID | None, Query()] = None,
    actor_id: Annotated[uuid.UUID | None, Query()] = None,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    cursor: Annotated[str | None, Query(description="上一頁回傳的 next_cursor（不透明）")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> AuditLogListOut:
    page = await run_orm(
        _service.query,
        principal.tenant_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        actor_id=actor_id,
        start=start,
        end=end,
        limit=limit,
        cursor=cursor,
    )
    return AuditLogListOut(
        items=[AuditLogOut(**asdict(record)) for record in page.items],
        next_cursor=page.next_cursor,
    )

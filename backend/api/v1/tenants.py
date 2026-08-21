"""`/tenants/current` 端點（09 §2.2）。

**路徑上沒有 id 是刻意的**：租戶來自 token，不是參數。設計成 ``/tenants/{id}``
的話就多出一個「傳別人的 id 會怎樣」的攻擊面，而那條路要靠每個端點自己記得檢查。
沒有 id 就沒有那個問題。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends

from api.dependencies.auth import Principal
from api.dependencies.permissions import RequireScope
from api.schemas.problem import ERROR_RESPONSES
from api.schemas.tenants import QuotaItemOut, QuotaOut, TenantOut, TenantUpdateIn
from core.db import run_orm
from services.identity.tenants import TenantService
from services.platform.quota import QuotaService

router = APIRouter(prefix="/tenants", tags=["tenants"], responses=ERROR_RESPONSES)
_service = TenantService()
_quota = QuotaService()


@router.get("/current", operation_id="tenants_get_current")
async def get_current(
    principal: Annotated[Principal, Depends(RequireScope("tenant:read"))],
) -> TenantOut:
    tenant = await run_orm(_service.get_current, principal.tenant_id)
    return TenantOut(**asdict(tenant))


@router.get("/current/quota", operation_id="tenants_get_current_quota")
async def get_current_quota(
    principal: Annotated[Principal, Depends(RequireScope("tenant:read"))],
) -> QuotaOut:
    """配額與用量的即時狀態（09 §2.2）——使用者在被 429 之前該有地方看到「快用完了」。"""
    statuses = await run_orm(_quota.status, principal.tenant_id)
    return QuotaOut(items=[QuotaItemOut(**asdict(status)) for status in statuses])


@router.patch("/current", operation_id="tenants_update_current")
async def update_current(
    payload: TenantUpdateIn,
    principal: Annotated[Principal, Depends(RequireScope("tenant:admin"))],
) -> TenantOut:
    tenant = await run_orm(_service.update_current, principal.tenant_id, name=payload.name)
    return TenantOut(**asdict(tenant))

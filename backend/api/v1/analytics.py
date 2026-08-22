"""`/analytics` 端點（09 §2.6，2A-3）。

Controller 三行原則：解析參數 → 一個 service 呼叫 → 回傳。資料一律來自彙總表
（理由見 services/platform/analytics.py）；權限 `analytics:read` 只給 owner/admin。

`from`／`to` 是查詢參數的原名（09 §2.6），但 `from` 是 Python 保留字——參數名
用 alias 對映。預設近 30 天：Dashboard 開頁不帶參數也要有東西看。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from api.dependencies.auth import Principal
from api.dependencies.permissions import RequireScope
from api.schemas.analytics import CostBucketOut, CostsOut, UsageBucketOut, UsageOut
from api.schemas.problem import ERROR_RESPONSES
from core.db import run_orm
from services.platform.analytics import AnalyticsService

router = APIRouter(prefix="/analytics", tags=["analytics"], responses=ERROR_RESPONSES)
_service = AnalyticsService()

_GroupBy = Literal["day", "user", "model", "category"]


def _range(start: date | None, end: date | None) -> tuple[date, date]:
    today = datetime.now(UTC).date()
    return start or today - timedelta(days=30), end or today


@router.get("/usage", operation_id="analytics_usage")
async def usage(
    principal: Annotated[Principal, Depends(RequireScope("analytics:read"))],
    group_by: Annotated[_GroupBy, Query()] = "day",
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> UsageOut:
    start, end = _range(start, end)
    buckets = await run_orm(
        _service.usage_summary, principal.tenant_id, start=start, end=end, group_by=group_by
    )
    return UsageOut(
        items=[
            UsageBucketOut(
                key=bucket.key,
                requests=bucket.requests,
                prompt_tokens=bucket.prompt_tokens,
                completion_tokens=bucket.completion_tokens,
            )
            for bucket in buckets
        ]
    )


@router.get("/costs", operation_id="analytics_costs")
async def costs(
    principal: Annotated[Principal, Depends(RequireScope("analytics:read"))],
    group_by: Annotated[Literal["day", "model", "category"], Query()] = "day",
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> CostsOut:
    start, end = _range(start, end)
    buckets = await run_orm(
        _service.usage_summary, principal.tenant_id, start=start, end=end, group_by=group_by
    )
    return CostsOut(items=[CostBucketOut(key=bucket.key, cost=bucket.cost) for bucket in buckets])

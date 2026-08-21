"""Platform context 的資料存取（05 §3.3，2A-1／2A-2b）。"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from django.db import models

from apps.platform.models import QuotaCounter, UsageLog
from core.tenant import get_current_tenant_id
from core.uow import unit_of_work
from repositories.base import TenantScopedRepository


class UsageLogRepository(TenantScopedRepository[UsageLog]):
    """usage_logs 的唯一寫入口（append-only：只有 add 與查詢，沒有改與刪）。"""

    model = UsageLog

    def add(
        self,
        *,
        category: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: Decimal | None,
        request_id: str,
        user_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> UsageLog:
        # 自帶交易而不是搭呼叫端的（**與其他 repository 的慣例相反，刻意的**）：
        # usage 落地是旁路，UsageService 會把這裡的失敗吞掉——若共用呼叫端的交易，
        # 這裡一炸整個交易就進 aborted 狀態，吞掉例外反而讓主流程的後續寫入全部
        # 失敗。交易同時負責設 RLS 的 GUC（core/uow 的兩件事綁定）。
        #
        # tenant 由 context 注入（鐵則 4）：缺 context 時 Fail Fast，
        # 不接受呼叫端自報 tenant_id。
        with unit_of_work():
            return UsageLog.objects.create(
                tenant_id=get_current_tenant_id(operation="UsageLogRepository.add"),
                category=category,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost=cost,
                request_id=request_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )

    def llm_token_total(self, *, since: datetime.datetime) -> int:
        """當期 llm 消費的 token 總和——tokens_month 對帳的事實來源（2A-2b）。"""
        row = (
            self.get_queryset()
            .filter(category="llm", created_at__gte=since)
            .aggregate(total=models.Sum(models.F("prompt_tokens") + models.F("completion_tokens")))
        )
        return int(row["total"] or 0)


class QuotaCounterRepository(TenantScopedRepository[QuotaCounter]):
    """對帳快照的 upsert——同一期永遠一列（uq_quotacounter_period）。"""

    model = QuotaCounter

    def upsert(
        self,
        *,
        resource: str,
        period: str,
        period_start: datetime.date,
        used: int,
        limit: int | None,
    ) -> QuotaCounter:
        row, _ = QuotaCounter.objects.update_or_create(
            tenant_id=get_current_tenant_id(operation="QuotaCounterRepository.upsert"),
            resource=resource,
            period=period,
            period_start=period_start,
            defaults={"used": used, "limit": limit},
        )
        return row

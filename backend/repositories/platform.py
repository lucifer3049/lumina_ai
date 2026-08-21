"""Platform context 的資料存取（05 §3.3，2A-1）。"""

from __future__ import annotations

import uuid
from decimal import Decimal

from apps.platform.models import UsageLog
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

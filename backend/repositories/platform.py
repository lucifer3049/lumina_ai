"""Platform context 的資料存取（05 §3.3，2A-1／2A-2b／2A-3／2A-4）。"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import cast

from django.db import models

from apps.platform.models import AuditLog, QuotaCounter, UsageDaily, UsageLog
from common.cursors import CursorError
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

    def daily_buckets(self, *, day: datetime.date) -> list[dict[str, object]]:
        """某一天的消費，按 (user, category, model) 分組——rollup 的輸入（2A-3）。

        日界以 UTC 切（created_at 是 timestamptz，聚合鍵取其 UTC 日期）；cost 的
        Sum 天然跳過 NULL（缺價目的呼叫計入 requests/tokens、不計入 cost）。
        """
        start = datetime.datetime.combine(day, datetime.time.min, tzinfo=datetime.UTC)
        end = start + datetime.timedelta(days=1)
        # cast：django-stubs 對 values().annotate() 推導出 TypedDict，與宣告的
        # dict[str, object] 不相容——實際執行期就是普通 dict。
        return cast(
            "list[dict[str, object]]",
            list(
                self.get_queryset()
                .filter(created_at__gte=start, created_at__lt=end)
                .values("user_id", "category", "model")
                .annotate(
                    requests=models.Count("id"),
                    prompt_tokens=models.Sum("prompt_tokens"),
                    completion_tokens=models.Sum("completion_tokens"),
                    cost=models.Sum("cost"),
                )
            ),
        )


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


class UsageDailyRepository(TenantScopedRepository[UsageDaily]):
    """日彙總的 upsert 與報表查詢（2A-3）。"""

    model = UsageDaily

    def upsert_bucket(
        self,
        *,
        day: datetime.date,
        user_id: uuid.UUID | None,
        category: str,
        model: str,
        requests: int,
        prompt_tokens: int,
        completion_tokens: int,
        cost: Decimal | None,
    ) -> None:
        UsageDaily.objects.update_or_create(
            tenant_id=get_current_tenant_id(operation="UsageDailyRepository.upsert_bucket"),
            day=day,
            user_id=user_id,
            category=category,
            model=model,
            defaults={
                "requests": requests,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost": cost,
            },
        )

    def summarize(
        self, *, start: datetime.date, end: datetime.date, group_by: str
    ) -> list[dict[str, object]]:
        """報表分組加總。`group_by` 由 API 層以 Literal 驗過——這裡的對照表是
        第二道防線（任意字串進 values() 等於任意欄位探測）。"""
        field = {"day": "day", "user": "user_id", "model": "model", "category": "category"}[
            group_by
        ]
        return list(
            self.get_queryset()
            .filter(day__gte=start, day__lte=end)
            .values(field)
            .annotate(
                requests=models.Sum("requests"),
                prompt_tokens=models.Sum("prompt_tokens"),
                completion_tokens=models.Sum("completion_tokens"),
                cost=models.Sum("cost"),
            )
            .order_by(field)
        )


class AuditLogRepository(TenantScopedRepository[AuditLog]):
    """稽核紀錄（2A-4）。**只有 add 與查詢**——沒有改、沒有刪。

    這不是「還沒實作」：`platform_auditlog` 上有 BEFORE UPDATE OR DELETE 的
    trigger（migration 0005），寫了也會被資料庫擋回來。到期清理走分區 DROP。
    """

    model = AuditLog

    def add(
        self,
        *,
        action: str,
        resource_type: str,
        outcome: str,
        request_id: str,
        actor_id: uuid.UUID | None = None,
        actor_type: str = "user",
        resource_id: uuid.UUID | None = None,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
        status: int | None = None,
        permission: str | None = None,
        ip: str | None = None,
        user_agent: str = "",
    ) -> AuditLog:
        # 自帶交易，理由同 `UsageLogRepository.add`：稽核是旁路，AuditService 會把
        # 這裡的失敗吞掉——共用呼叫端的交易時，這裡一炸會讓整個交易進 aborted，
        # 於是「稽核記不成」變成「使用者的操作也失敗」，方向完全相反。
        #
        # 另一個理由是**時序**：middleware 是在回應送出之後才寫這一列，那時業務
        # 交易早就 commit 了，本來就不可能共用。
        with unit_of_work():
            return AuditLog.objects.create(
                tenant_id=get_current_tenant_id(operation="AuditLogRepository.add"),
                action=action,
                resource_type=resource_type,
                outcome=outcome,
                request_id=request_id,
                actor_id=actor_id,
                actor_type=actor_type,
                resource_id=resource_id,
                before=before,
                after=after,
                status=status,
                permission=permission,
                ip=ip,
                user_agent=user_agent,
            )

    def page(
        self,
        *,
        limit: int,
        cursor: dict[str, object] | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        start: datetime.datetime | None = None,
        end: datetime.datetime | None = None,
    ) -> tuple[list[AuditLog], dict[str, object] | None]:
        """新到舊的一頁 + 下一頁的游標。

        排序鍵是 **(created_at, id)** 而不只是 created_at：稽核列的時間戳會撞
        （同一次請求寫的批次、同一秒的大量嘗試），只用時間當游標會讓那些列在翻頁
        時消失或重複——而查稽核的人正是在數「他到底試了幾次」。

        游標比較用 `Q(created_at__lt) | Q(created_at=, id__lt)`，不用 tuple 比較：
        Django ORM 沒有 row-value 語法，而手寫 SQL 會繞過 `get_queryset()` 的
        租戶 filter（鐵則 4 的第一道防線）。
        """
        query = self.get_queryset()
        if action is not None:
            query = query.filter(action=action)
        if resource_type is not None:
            query = query.filter(resource_type=resource_type)
        if resource_id is not None:
            query = query.filter(resource_id=resource_id)
        if actor_id is not None:
            query = query.filter(actor_id=actor_id)
        if start is not None:
            query = query.filter(created_at__gte=start)
        if end is not None:
            query = query.filter(created_at__lt=end)
        if cursor is not None:
            key, last_id = _cursor_key(cursor)
            query = query.filter(
                models.Q(created_at__lt=key)
                | models.Q(created_at=key, id__lt=last_id)  # 同一時間戳的第二頁
            )

        # 多取一筆是判斷「還有沒有下一頁」唯一可靠的方法（同 repositories/conversation.py
        # 的 `_split_page`；第三個需要它的地方出現時就搬進 repositories/base.py）。
        rows = list(query.order_by("-created_at", "-id")[: limit + 1])
        if len(rows) <= limit:
            return rows, None
        page = rows[:limit]
        last = page[-1]
        return page, {"k": last.created_at.isoformat(), "id": str(last.id)}


def _cursor_key(cursor: dict[str, object]) -> tuple[datetime.datetime, uuid.UUID]:
    """游標 dict → 排序鍵。內容不對時 `CursorError`，由 service 轉 422
    （形狀同 `repositories/conversation.py`）。"""
    try:
        return datetime.datetime.fromisoformat(str(cursor["k"])), uuid.UUID(str(cursor["id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise CursorError("游標內容不正確") from exc

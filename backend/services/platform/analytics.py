"""Analytics——usage 的彙總與報表（04 §8.2，2A-3）。

兩半：`UsageRollupService` 每小時把 usage_logs 壓成 usage_daily（Beat），
`AnalyticsService` 回答 Dashboard 的兩個問題（用了多少、花了多少）。

報表**只讀彙總表**：usage_logs 是按月分區的高成長表，「過去 90 天按模型」要掃
整月分區。代價是「今天」的數字最多晚一小時——即時的那份在
`/tenants/current/quota`，兩者分工明確。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

from config.logging import get_logger
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import TenantDirectoryRepository
from repositories.platform import UsageDailyRepository, UsageLogRepository

logger = get_logger(__name__)

__all__ = ["GROUP_BY_CHOICES", "AnalyticsService", "UsageBucket", "UsageRollupService"]

# API 契約的一部分（api/v1/analytics.py 以 Literal 驗）。
GROUP_BY_CHOICES = ("day", "user", "model", "category")


class UsageRollupService:
    def __init__(
        self,
        *,
        usage_logs: UsageLogRepository | None = None,
        daily: UsageDailyRepository | None = None,
        directory: TenantDirectoryRepository | None = None,
    ) -> None:
        self._usage_logs = usage_logs or UsageLogRepository()
        self._daily = daily or UsageDailyRepository()
        self._directory = directory or TenantDirectoryRepository()

    def rollup_tenant(self, tenant_id: uuid.UUID, day: date) -> int:
        """把一個租戶某一天的 usage 壓進彙總表；回傳寫入的格子數。

        從 usage_logs **重算**而不是增量累加：重算天然冪等（Beat 重跑、手動補跑
        都安全），增量則要記「上次算到哪」——那個游標本身就是新的漂移來源。
        """
        with tenant_context(tenant_id), unit_of_work():
            buckets = self._usage_logs.daily_buckets(day=day)
            for bucket in buckets:
                self._daily.upsert_bucket(
                    day=day,
                    user_id=cast("uuid.UUID | None", bucket["user_id"]),
                    category=str(bucket["category"]),
                    model=str(bucket["model"]),
                    requests=int(cast("int", bucket["requests"])),
                    prompt_tokens=int(cast("int", bucket["prompt_tokens"] or 0)),
                    completion_tokens=int(cast("int", bucket["completion_tokens"] or 0)),
                    cost=cast("Decimal | None", bucket["cost"]),
                )
        return len(buckets)

    def rollup_all(self) -> int:
        """逐 active 租戶，蓋**昨天＋今天**（Beat 每小時）；回傳處理的租戶數。

        昨天也要蓋：午夜前最後一小時的量，只跑今天的話永遠缺——每天第一輪把
        昨天補到終值。單一租戶失敗不中斷整輪（同對帳的理由）。
        """
        today = datetime.now(UTC).date()
        processed = 0
        for tenant_id in self._directory.active_tenant_ids():
            try:
                for day in (today - timedelta(days=1), today):
                    self.rollup_tenant(tenant_id, day)
            except Exception:
                logger.exception("usage_rollup_failed", tenant_id=str(tenant_id))
            else:
                processed += 1
        return processed


@dataclass(frozen=True, slots=True)
class UsageBucket:
    key: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    cost: Decimal | None


class AnalyticsService:
    def __init__(self, *, daily: UsageDailyRepository | None = None) -> None:
        self._daily = daily or UsageDailyRepository()

    def usage_summary(
        self, tenant_id: uuid.UUID, *, start: date, end: date, group_by: str
    ) -> list[UsageBucket]:
        with tenant_context(tenant_id), unit_of_work():
            rows = self._daily.summarize(start=start, end=end, group_by=group_by)
        field = {"day": "day", "user": "user_id", "model": "model", "category": "category"}[
            group_by
        ]
        return [
            UsageBucket(
                key=str(row[field]),
                requests=int(cast("int", row["requests"] or 0)),
                prompt_tokens=int(cast("int", row["prompt_tokens"] or 0)),
                completion_tokens=int(cast("int", row["completion_tokens"] or 0)),
                cost=cast("Decimal | None", row["cost"]),
            )
            for row in rows
        ]

"""Quota 日結對帳（04 §8.1「reserve/commit + Redis 計數 + 對帳」的第三段，2A-2b）。

Redis 計數器是**推測**（即時 reserve/commit 的結果），DB 是**事實**。兩者會漂：
Redis flush、行程死在 commit 之前、存量檢查的併發競態（2A-2a 記錄在案）。這裡
每天把推測擺回事實，並在 quota_counters 留下耐久快照（Analytics 與稽核讀它，
不去問一個會過期的 key）。

事實來源按資源各自的（tests/integration/test_quota_reconciliation.py 的表）：
tokens_month ← usage_logs；messages_day ← assistant 訊息；documents／
storage_bytes ← 文件表聚合；streams 不對帳（瞬時值沒有「應該是多少」）。

**方向鐵律：DB 蓋 Redis。** 快照也只寫「從事實算出來的值」——拿 Redis 的數字
落 DB 等於把漂移拍照存證。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from config.logging import get_logger
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.conversation import MessageRepository
from repositories.identity import TenantDirectoryRepository
from repositories.knowledge import DocumentRepository
from repositories.platform import QuotaCounterRepository, UsageLogRepository
from services.platform.quota import QuotaService

logger = get_logger(__name__)

__all__ = ["QuotaReconciliationService"]


@dataclass(frozen=True, slots=True)
class _Fact:
    resource: str
    period: str  # day / month
    used: int
    corrects_redis: bool


class QuotaReconciliationService:
    def __init__(
        self,
        *,
        usage_logs: UsageLogRepository | None = None,
        messages: MessageRepository | None = None,
        documents: DocumentRepository | None = None,
        counters: QuotaCounterRepository | None = None,
        directory: TenantDirectoryRepository | None = None,
        quota: QuotaService | None = None,
    ) -> None:
        self._usage_logs = usage_logs or UsageLogRepository()
        self._messages = messages or MessageRepository()
        self._documents = documents or DocumentRepository()
        self._counters = counters or QuotaCounterRepository()
        self._directory = directory or TenantDirectoryRepository()
        self._quota = quota or QuotaService()

    def reconcile_all(self) -> int:
        """逐 active 租戶對帳；回傳處理的租戶數。

        單一租戶失敗不中斷整輪——漏掉誰、誰的計數器就永遠不會被校正，而別人
        不該陪葬。失敗記 log（含 tenant id），下一輪日結自然重試。
        """
        processed = 0
        for tenant_id in self._directory.active_tenant_ids():
            try:
                self.reconcile_tenant(tenant_id)
            except Exception:
                logger.exception("quota_reconcile_failed", tenant_id=str(tenant_id))
            else:
                processed += 1
        return processed

    def reconcile_tenant(self, tenant_id: uuid.UUID) -> None:
        now = datetime.now(UTC)
        month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
        day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)

        with tenant_context(tenant_id), unit_of_work():
            facts = [
                _Fact(
                    "tokens_month",
                    "month",
                    self._usage_logs.llm_token_total(since=month_start),
                    corrects_redis=True,
                ),
                _Fact(
                    "messages_day",
                    "day",
                    self._messages.assistant_count_since(day_start),
                    corrects_redis=True,
                ),
                _Fact("documents", "day", self._documents.active_count(), corrects_redis=False),
                _Fact(
                    "storage_bytes",
                    "day",
                    self._documents.active_size_bytes(),
                    corrects_redis=False,
                ),
            ]

        limits = self._quota.limits(tenant_id)
        for fact in facts:
            period_start = (month_start if fact.period == "month" else day_start).date()
            with tenant_context(tenant_id), unit_of_work():
                self._counters.upsert(
                    resource=fact.resource,
                    period=fact.period,
                    period_start=period_start,
                    used=fact.used,
                    limit=limits.get(fact.resource),
                )
            if fact.corrects_redis:
                self._quota.correct(tenant_id, fact.resource, fact.used)
        logger.info(
            "quota_reconciled",
            tenant_id=str(tenant_id),
            **{fact.resource: fact.used for fact in facts},
        )

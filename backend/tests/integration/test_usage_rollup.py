"""驗收：usage 日彙總（04 §8.2「消費 usage 事件 → 彙總表」，13 §4 工作包 2A-3）。

`/analytics/*` 不直接掃 usage_logs：那是按月分區的高成長表，「過去 90 天按模型」
這種查詢要掃三個分區的全部列。彙總表把它壓成 (tenant, 日, user, category, model)
一列，Dashboard 的每一個問題都變成小表上的 GROUP BY。

rollup 每小時跑（今天的數字最多晚一小時，Dashboard 可接受；即時的那份在
`/tenants/current/quota`）。**維度缺 kb**：usage_logs 沒有 kb_id（chat 是跨 KB
的對話），04 §8.2 的 per-kb 彙總記為缺口、待 3B 評測需要時補欄位。

三件事錯了都不會有例外：

1. **重跑翻倍**。rollup 是 Beat 排程，重跑必然發生——同一格必須是 upsert，
   否則報表數字隨重跑次數成長。
2. **缺價目的列被丟掉**。cost 是 None 的呼叫仍然是一次呼叫、一批 token——
   requests 與 token 照算，只有 cost 欄跳過未知值。
3. **日界切錯**。昨天 23:59 的呼叫進了今天的格子，跨日對帳永遠差一點。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from services.platform.analytics import UsageRollupService

from apps.platform.models import UsageDaily, UsageLog
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)

_TODAY = datetime.now(UTC).date()
_USER = uuid.uuid4()


@pytest.fixture
def tenants() -> None:
    for tenant_id, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=slug)


def _seed(
    tenant_id: uuid.UUID,
    *,
    model: str = "mock-chat",
    category: str = "llm",
    prompt: int = 100,
    completion: int = 50,
    cost: Decimal | None = Decimal("0.010000"),
    user_id: uuid.UUID | None = _USER,
    days_ago: int = 0,
) -> None:
    with tenant_scope(tenant_id):
        row = UsageLog.objects.create(
            tenant_id=tenant_id,
            user_id=user_id,
            category=category,
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cost=cost,
            request_id=str(uuid.uuid4()),
        )
        if days_ago:
            UsageLog.objects.filter(id=row.id, created_at=row.created_at).update(
                created_at=row.created_at - timedelta(days=days_ago)
            )


def _rows(tenant_id: uuid.UUID) -> list[UsageDaily]:
    with tenant_scope(tenant_id):
        return list(UsageDaily.objects.filter(tenant_id=tenant_id).order_by("model"))


class TestRollup:
    def test_one_bucket_per_user_category_model(self, tenants: None) -> None:
        _seed(TENANT_A, model="mock-chat", prompt=100, completion=50)
        _seed(TENANT_A, model="mock-chat", prompt=30, completion=20)
        _seed(TENANT_A, model="mock-embedding", category="embedding", user_id=None, completion=0)

        UsageRollupService().rollup_tenant(TENANT_A, _TODAY)

        rows = {(row.model, row.category): row for row in _rows(TENANT_A)}
        chat = rows[("mock-chat", "llm")]
        assert chat.requests == 2
        assert chat.prompt_tokens == 130
        assert chat.completion_tokens == 70
        assert chat.cost == Decimal("0.020000")
        assert chat.user_id == _USER
        embed = rows[("mock-embedding", "embedding")]
        assert embed.requests == 1
        assert embed.user_id is None

    def test_unknown_costs_do_not_drop_the_rows(self, tenants: None) -> None:
        """兩筆呼叫、一筆沒價目：requests=2、tokens 全算，cost 只加已知的那筆。"""
        _seed(TENANT_A, cost=Decimal("0.010000"))
        _seed(TENANT_A, cost=None)

        UsageRollupService().rollup_tenant(TENANT_A, _TODAY)

        row = _rows(TENANT_A)[0]
        assert row.requests == 2
        assert row.prompt_tokens == 200
        assert row.cost == Decimal("0.010000")

    def test_rerunning_updates_in_place(self, tenants: None) -> None:
        service = UsageRollupService()
        _seed(TENANT_A)
        service.rollup_tenant(TENANT_A, _TODAY)
        _seed(TENANT_A)

        service.rollup_tenant(TENANT_A, _TODAY)

        rows = _rows(TENANT_A)
        assert len(rows) == 1
        assert rows[0].requests == 2

    def test_only_that_days_rows_are_aggregated(self, tenants: None) -> None:
        _seed(TENANT_A, days_ago=0)
        _seed(TENANT_A, days_ago=1)

        UsageRollupService().rollup_tenant(TENANT_A, _TODAY)

        rows = _rows(TENANT_A)
        assert len(rows) == 1
        assert rows[0].day == _TODAY
        assert rows[0].requests == 1, "昨天的呼叫不得進今天的格子"

    def test_rollup_all_covers_tenants_and_both_days(self, tenants: None) -> None:
        """Beat 的每一輪蓋「昨天＋今天」：昨天要補到終值（午夜前最後一小時的量
        只跑今天的話永遠缺），今天要保持新鮮。"""
        _seed(TENANT_A, days_ago=1)
        _seed(TENANT_B, days_ago=0)

        UsageRollupService().rollup_all()

        assert [row.day for row in _rows(TENANT_A)] == [_TODAY - timedelta(days=1)]
        assert [row.day for row in _rows(TENANT_B)] == [_TODAY]


class TestTable:
    def test_the_table_has_forced_rls_with_policy(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
                ["platform_usagedaily"],
            )
            enabled, forced = cursor.fetchone()
            cursor.execute(
                "SELECT count(*) FROM pg_policies "
                "WHERE tablename = %s AND policyname = 'tenant_isolation'",
                ["platform_usagedaily"],
            )
            policies = cursor.fetchone()[0]

        assert enabled and forced, "platform_usagedaily 沒有 FORCE RLS"
        assert policies == 1

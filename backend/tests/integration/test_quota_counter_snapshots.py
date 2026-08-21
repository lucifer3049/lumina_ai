"""驗收：quota_counters 對帳快照表（05 §3.3，13 §4 工作包 2A-2b）。

即時計數在 Redis（2A-2a），這張表是它的**耐久影子**：日結對帳把「事實來源算出來
的用量」落到 DB。兩個讀者：①Redis 被 flush／漂移時，對帳能把計數器擺回正確值；
②Analytics（2A-3）與帳務稽核要查歷史用量，不能去問一個會過期的 key。

普通表、不分區：一天每租戶最多幾列（資源 × 期別），成長率與 usage_logs 差三個
數量級，套分區是純粹的複雜度。

RLS 一併在此驗（同 test_rls_platform.py 的方法論，不重述）：快照的內容是**用量
輪廓**，與 usage_logs 同級敏感。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from django.db import connection
from django.db.utils import IntegrityError

from apps.platform.models import QuotaCounter
from core.tenant import tenant_context
from core.uow import unit_of_work
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)

TABLE = "platform_quotacounter"


@pytest.fixture
def tenants() -> None:
    for tenant_id, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=slug)


def _snapshot(tenant_id: uuid.UUID, **overrides: object) -> QuotaCounter:
    fields: dict[str, object] = {
        "tenant_id": tenant_id,
        "resource": "tokens_month",
        "period": "month",
        "period_start": datetime.now(UTC).date().replace(day=1),
        "used": 12345,
        "limit": 1_000_000,
    }
    fields.update(overrides)
    return QuotaCounter.objects.create(**fields)


class TestSchema:
    def test_a_row_round_trips(self, tenants: None) -> None:
        with tenant_scope(TENANT_A):
            _snapshot(TENANT_A)
            row = QuotaCounter.objects.get(tenant_id=TENANT_A)

        assert row.used == 12345
        assert row.limit == 1_000_000
        assert row.period == "month"

    def test_limit_may_be_null(self, tenants: None) -> None:
        """不限制的資源快照時 limit 存 NULL——存 0 或省略列都會讓歷史報表把
        「不限制」讀成「禁止」或「沒用過」。"""
        with tenant_scope(TENANT_A):
            _snapshot(TENANT_A, resource="documents", period="day", limit=None)

            assert QuotaCounter.objects.filter(limit__isnull=True).count() == 1

    def test_the_snapshot_key_is_unique(self, tenants: None) -> None:
        """(tenant, resource, period, period_start) 唯一（05 §3.3 的複合 PK 語意）。
        沒有它，對帳每天插新列，同一期的快照會有好幾個版本，報表隨 join 條件跳動。"""
        with tenant_scope(TENANT_A):
            _snapshot(TENANT_A)
            with pytest.raises(IntegrityError):
                _snapshot(TENANT_A, used=99999)


class TestRls:
    def test_the_table_has_forced_rls_with_policy(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
                [TABLE],
            )
            enabled, forced = cursor.fetchone()
            cursor.execute(
                "SELECT qual, with_check FROM pg_policies "
                "WHERE tablename = %s AND policyname = 'tenant_isolation'",
                [TABLE],
            )
            policy = cursor.fetchone()

        assert enabled and forced, f"{TABLE} 沒有 FORCE RLS"
        assert policy is not None and all("app.tenant_id" in part for part in policy)

    def test_each_tenant_only_sees_its_own_snapshots(self, tenants: None) -> None:
        for tenant_id in (TENANT_A, TENANT_B):
            with tenant_scope(tenant_id):
                _snapshot(tenant_id)

        with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
            cursor.execute(f"SELECT tenant_id FROM {TABLE}")  # noqa: S608
            visible = {row[0] for row in cursor.fetchall()}

        assert visible == {TENANT_A}

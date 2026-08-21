"""驗收：usage_logs 資料層（05 §3.3／§4／§5.2，13 §4 工作包 2A-1）。

`platform_usagelog` 是 05 §5.2 點名的三張高成長表之二（`messages` 已在 1D-1 建成
分區表，`audit_logs` 排 2A-4）。**每一次 LLM 呼叫、每一批 embedding 都是一列**，
它的成長速度只會比 messages 快——分區同樣是 day-1 的不可逆決定。

方法論同 tests/integration/test_conversation_models.py，不重述：驗的全是 DB 性質
（分區、PK、索引），用假物件驗這一層等於什麼都沒驗。

RLS 在 tests/integration/test_rls_platform.py（行為驗證，不是查目錄）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.db import connection

from apps.platform.models import UsageLog
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)

# 同 conversation 的守門：剩餘不足 3 個月先紅（05 §5.2 的 Beat 已在本包建立，
# 這條測試是 Beat 掛掉時的第二道防線）。
MIN_MONTHS_AHEAD = 3


def _one(sql: str) -> tuple[object, ...] | None:
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchone()


class TestPartitioning:
    def test_usage_logs_is_a_partitioned_table(self) -> None:
        row = _one("SELECT relkind FROM pg_class WHERE relname = 'platform_usagelog'")

        assert row is not None, "platform_usagelog 不存在"
        assert row[0] == "p", f"不是分區表（relkind={row[0]}）"

    def test_it_is_partitioned_by_created_at(self) -> None:
        row = _one(
            "SELECT pg_get_partkeydef(oid) FROM pg_class WHERE relname = 'platform_usagelog'"
        )

        assert row is not None
        assert "created_at" in str(row[0])

    def test_the_partition_key_is_in_the_primary_key(self) -> None:
        """PostgreSQL 的硬性要求；有人為了讓 migration 過而把 PK 縮成只剩
        created_at 時，id 不再唯一——症狀等到同一微秒兩列才出現。"""
        row = _one(
            """
            SELECT array_agg(a.attname ORDER BY a.attnum)
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'platform_usagelog'::regclass AND i.indisprimary
            """
        )

        assert row is not None
        assert set(row[0]) == {"id", "created_at"}

    def test_there_is_no_default_partition(self) -> None:
        """有 DEFAULT 的話，超出範圍的資料安靜地全部擠進去，分區等於沒有作用；
        沒有的話 INSERT 直接失敗——那是看得見的（同 1D-1 的決定）。"""
        row = _one(
            """
            SELECT count(*)
            FROM pg_partitioned_table pt
            JOIN pg_class c ON c.oid = pt.partdefid
            WHERE pt.partrelid = 'platform_usagelog'::regclass
            """
        )

        assert row is not None and row[0] == 0

    def test_enough_future_partitions_exist(self) -> None:
        """剩餘涵蓋不足 3 個月時先紅——訊號出現在 CI 上，比出現在「每一次 LLM 呼叫
        都寫入失敗」上早好幾個月。"""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT max(
                    (regexp_match(c.relname, '(\\d{4})_(\\d{2})$'))[1]::int * 12
                    + (regexp_match(c.relname, '(\\d{4})_(\\d{2})$'))[2]::int
                )
                FROM pg_inherits i
                JOIN pg_class c ON c.oid = i.inhrelid
                WHERE i.inhparent = 'platform_usagelog'::regclass
                """
            )
            row = cursor.fetchone()

        assert row is not None and row[0] is not None, "一個分區都沒有"
        now = datetime.now(UTC)
        months_ahead = row[0] - (now.year * 12 + now.month)
        assert months_ahead >= MIN_MONTHS_AHEAD, (
            f"未來分區只剩 {months_ahead} 個月，該建下一批了"
        )


class TestIndexes:
    def test_the_design_document_indexes_exist(self) -> None:
        """05 §4：(tenant_id, created_at) 給對帳與統計、(request_id) 給追蹤。
        建在父表上才會傳播到之後新建的分區。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'platform_usagelog'"
            )
            defs = [row[0] for row in cursor.fetchall()]

        assert any("tenant_id, created_at" in d for d in defs), defs
        assert any("(request_id" in d.replace("USING btree ", "") for d in defs), defs


class TestColumns:
    def test_a_row_round_trips(self) -> None:
        """欄位齊全且型別對（05 §3.3）：cost 是 numeric 不是 float，
        user/conversation 可空（embedding 是系統行為，沒有使用者）。"""
        with tenant_scope(TENANT_A):
            make_tenant(id=TENANT_A, slug="tenant-a")
            UsageLog.objects.create(
                tenant_id=TENANT_A,
                user_id=None,
                category="embedding",
                model="mock-embedding",
                prompt_tokens=1234,
                completion_tokens=0,
                cost=Decimal("0.000025"),
                conversation_id=None,
                request_id=f"embed:{uuid.uuid4()}",
            )
            row = UsageLog.objects.get(tenant_id=TENANT_A)

        assert row.cost == Decimal("0.000025")
        assert row.user_id is None
        assert row.created_at is not None

    def test_cost_may_be_null(self) -> None:
        """None 是「還不知道」——缺價目的列必須存得進去（理由見
        tests/unit/test_usage_service.py）。"""
        with tenant_scope(TENANT_A):
            make_tenant(id=TENANT_A, slug="tenant-a")
            UsageLog.objects.create(
                tenant_id=TENANT_A,
                category="llm",
                model="unpriced-model",
                prompt_tokens=10,
                completion_tokens=5,
                cost=None,
                request_id=str(uuid.uuid4()),
            )

            assert UsageLog.objects.filter(cost__isnull=True).count() == 1

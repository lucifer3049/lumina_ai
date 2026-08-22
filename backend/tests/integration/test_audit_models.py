"""驗收：audit_logs 資料層（05 §3.3／§4／§5.2、04 §8.3，13 §4 工作包 2A-4）。

`platform_auditlog` 是 05 §5.2 點名的三張高成長表的最後一張（`messages` 於 1D-1、
`usage_logs` 於 2A-1）。方法論同 `test_usage_models.py`，不重述：驗的全是 DB 性質
（分區、PK、索引、約束），用假物件驗這一層等於什麼都沒驗。

兩件 usage_logs 沒有的事：

1. **append-only 由資料庫擋，不是靠慣例**。稽核紀錄的價值全部來自「事後不能改」
   ——一份可以被 UPDATE 的稽核表，在真的出事時（有人越權、有人刪了不該刪的東西）
   完全不能當證據，而它平常看起來跟能當證據的那種一模一樣。Repository 沒有 update
   方法只擋得住走 Repository 的人；擋在 DB 上連 `manage.py shell` 都擋得住。
   保留政策的到期清理不受影響：那是 DROP/DETACH 整個分區（DDL），不是 DELETE。

2. **索引是 (tenant_id, resource_type, resource_id)**（05 §4）。稽核的兩種查法：
   「這段時間發生了什麼」（時間序）與「這份文件被誰動過」（資源序），後者少了索引
   就是掃整個月的分區。

RLS 在本檔末（行為驗證，不是查目錄），理由同 test_rls_platform.py。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import cast

import pytest
from django.db import DatabaseError, connection, transaction

from apps.platform.models import AuditLog
from services.platform.maintenance import PARTITIONED_TABLES
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)

TABLE = "platform_auditlog"

# 同 usage_logs 的守門：剩餘不足 3 個月先紅（05 §5.2 的 Beat 已在 2A-1 建立，
# 這條測試是 Beat 掛掉時的第二道防線）。
MIN_MONTHS_AHEAD = 3


def _one(sql: str) -> tuple[object, ...] | None:
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return cast("tuple[object, ...] | None", cursor.fetchone())


def _make_row(tenant_id: uuid.UUID, **overrides: object) -> AuditLog:
    fields: dict[str, object] = {
        "tenant_id": tenant_id,
        "actor_id": uuid.uuid4(),
        "actor_type": "user",
        "action": "knowledge_base.delete",
        "resource_type": "knowledge_base",
        "resource_id": uuid.uuid4(),
        "before": {"name": "舊名字"},
        "after": None,
        "outcome": "succeeded",
        "status": 204,
        "permission": None,
        "ip": "203.0.113.7",
        "user_agent": "pytest/1.0",
        "request_id": uuid.uuid4().hex,
    }
    fields.update(overrides)
    return AuditLog.objects.create(**fields)


class TestPartitioning:
    def test_audit_logs_is_a_partitioned_table(self) -> None:
        row = _one(f"SELECT relkind FROM pg_class WHERE relname = '{TABLE}'")  # noqa: S608

        assert row is not None, f"{TABLE} 不存在"
        assert row[0] == "p", f"不是分區表（relkind={row[0]}）"

    def test_it_is_partitioned_by_created_at(self) -> None:
        row = _one(f"SELECT pg_get_partkeydef(oid) FROM pg_class WHERE relname = '{TABLE}'")  # noqa: S608

        assert row is not None
        assert "created_at" in str(row[0])

    def test_the_partition_key_is_in_the_primary_key(self) -> None:
        row = _one(
            f"""
            SELECT array_agg(a.attname ORDER BY a.attnum)
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = '{TABLE}'::regclass AND i.indisprimary
            """  # noqa: S608 —— 表名是本檔常數
        )

        assert row is not None
        assert set(cast("list[str]", row[0])) == {"id", "created_at"}

    def test_there_is_no_default_partition(self) -> None:
        """有 DEFAULT 的話超出範圍的資料安靜地全部擠進去，分區等於沒有作用
        （同 1D-1／2A-1 的決定）。"""
        row = _one(
            f"""
            SELECT count(*)
            FROM pg_partitioned_table pt
            JOIN pg_class c ON c.oid = pt.partdefid
            WHERE pt.partrelid = '{TABLE}'::regclass
            """  # noqa: S608
        )

        assert row is not None and row[0] == 0

    def test_enough_future_partitions_exist(self) -> None:
        row = _one(
            f"""
            SELECT max(
                (regexp_match(c.relname, '(\\d{{4}})_(\\d{{2}})$'))[1]::int * 12
                + (regexp_match(c.relname, '(\\d{{4}})_(\\d{{2}})$'))[2]::int
            )
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            WHERE i.inhparent = '{TABLE}'::regclass
            """  # noqa: S608
        )

        assert row is not None and row[0] is not None, "一個分區都沒有"
        now = datetime.now(UTC)
        months_ahead = cast("int", row[0]) - (now.year * 12 + now.month)
        assert months_ahead >= MIN_MONTHS_AHEAD, f"未來分區只剩 {months_ahead} 個月"

    def test_it_is_registered_for_monthly_maintenance(self) -> None:
        """漏了註冊，Beat 每月建的是別的表的分區，這張表在第 12 個月之後
        **每一次寫入都失敗**——而寫入失敗的是稽核，主流程不會有任何症狀。"""
        assert TABLE in PARTITIONED_TABLES


class TestIndexes:
    def test_the_design_document_indexes_exist(self) -> None:
        """05 §4：(tenant_id, created_at) 給時間序查詢、
        (tenant_id, resource_type, resource_id) 給「這份資源被誰動過」。"""
        with connection.cursor() as cursor:
            cursor.execute("SELECT indexdef FROM pg_indexes WHERE tablename = %s", [TABLE])
            defs = [row[0] for row in cursor.fetchall()]

        assert any("tenant_id, created_at" in d for d in defs), defs
        assert any("tenant_id, resource_type, resource_id" in d for d in defs), defs


class TestAppendOnly:
    def test_updates_are_rejected_by_the_database(self) -> None:
        with tenant_scope(TENANT_A):
            make_tenant(id=TENANT_A, slug="tenant-a")
            row = _make_row(TENANT_A)

        with (
            tenant_scope(TENANT_A),
            pytest.raises(DatabaseError) as excinfo,
            transaction.atomic(),
        ):
            AuditLog.objects.filter(id=row.id).update(action="tampered")

        assert "append-only" in str(excinfo.value).lower()

    def test_deletes_are_rejected_by_the_database(self) -> None:
        with tenant_scope(TENANT_A):
            make_tenant(id=TENANT_A, slug="tenant-a")
            row = _make_row(TENANT_A)

        with (
            tenant_scope(TENANT_A),
            pytest.raises(DatabaseError) as excinfo,
            transaction.atomic(),
        ):
            AuditLog.objects.filter(id=row.id).delete()

        assert "append-only" in str(excinfo.value).lower()


class TestColumns:
    def test_a_row_round_trips(self) -> None:
        """05 §3.3 的欄位齊全且型別對：before/after 是 jsonb（可查詢、可空）、
        ip 是 inet、actor/resource 皆可空（系統行為沒有 actor）。"""
        with tenant_scope(TENANT_A):
            make_tenant(id=TENANT_A, slug="tenant-a")
            written = _make_row(TENANT_A)
            row = AuditLog.objects.get(id=written.id)

        assert row.before == {"name": "舊名字"}
        assert row.after is None
        assert str(row.ip) == "203.0.113.7"
        assert row.created_at is not None

    def test_system_actions_may_have_no_actor(self) -> None:
        """維運 job（分區維護、對帳）沒有使用者。actor_id NOT NULL 會逼呼叫端
        塞一個假 uuid，而那比留白更糟——它看起來像真的有人做了這件事。"""
        with tenant_scope(TENANT_A):
            make_tenant(id=TENANT_A, slug="tenant-a")
            _make_row(TENANT_A, actor_id=None, actor_type="system", resource_id=None)

            assert AuditLog.objects.filter(actor_type="system").count() == 1


class TestRowLevelSecurity:
    """稽核外洩的是**組織的內部行為**：誰在管理誰、什麼時候刪了什麼、
    哪些 IP 在嘗試越權。它比用量輪廓更敏感，而它同樣沒有任何洩漏症狀。"""

    def test_the_table_has_rls_enabled_and_forced(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
                [TABLE],
            )
            enabled, forced = cursor.fetchone()

        assert enabled, f"{TABLE} 沒有啟用 RLS"
        assert forced, f"{TABLE} 沒有 FORCE（owner 建的表對 owner 預設豁免 policy）"

    def test_every_partition_has_its_own_policy(self) -> None:
        """直接查子分區的人繞得過父表的 policy——每個分區都要自己開。"""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                FROM pg_inherits i
                JOIN pg_class c ON c.oid = i.inhrelid
                WHERE i.inhparent = %s::regclass
                """,
                [TABLE],
            )
            partitions = cursor.fetchall()

        assert partitions, "一個分區都沒有"
        unprotected = [name for name, enabled, forced in partitions if not (enabled and forced)]
        assert not unprotected, f"這些分區沒有 RLS：{unprotected}"

    def test_a_tenant_cannot_read_another_tenants_rows(self) -> None:
        for tenant_id, suffix in ((TENANT_A, "a"), (TENANT_B, "b")):
            with tenant_scope(tenant_id):
                make_tenant(id=tenant_id, slug=f"tenant-{suffix}")
                _make_row(tenant_id)

        with tenant_scope(TENANT_A):
            visible = {row.tenant_id for row in AuditLog.objects.all()}

        assert visible == {TENANT_A}

    def test_a_tenant_cannot_write_a_row_for_another_tenant(self) -> None:
        for tenant_id, suffix in ((TENANT_A, "a"), (TENANT_B, "b")):
            with tenant_scope(tenant_id):
                make_tenant(id=tenant_id, slug=f"tenant-{suffix}")

        with tenant_scope(TENANT_A), pytest.raises(DatabaseError), transaction.atomic():
            _make_row(TENANT_B)

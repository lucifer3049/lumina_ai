"""驗收：platform_usagelog 的 RLS（05 §5.1、10 §3，13 §4 工作包 2A-1）。

方法論同 test_rls_conversation.py，不重述：原生 SQL 繞過 Repository、讀寫分開驗。

**usage 洩漏的是租戶的消費輪廓**：誰在什麼時間用了多少 token、掛在哪個對話上。
它不含內容，但含**行為節奏**——一家公司的問答量暴增往往對應一件正在發生的事
（併購、裁員、訴訟），而那正是他們最不想讓外人推斷的東西。2A-3 的 Analytics
直接坐在這張表上，這裡漏了等於報表跨租戶。

新表照規矩加進 RLS 名單（test_rls_conversation.py 開頭的教訓：漏掉的表不會有
任何症狀，查詢照常回傳，只是範圍變成整個資料庫）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from django.db import connection

from apps.platform.models import UsageLog
from core.tenant import tenant_context
from core.uow import unit_of_work
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)

TABLE = "platform_usagelog"


def _raw_ids() -> set[uuid.UUID]:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT id FROM {TABLE}")  # noqa: S608 —— 表名是本檔常數
        return {row[0] for row in cursor.fetchall()}


def _usage_row(tenant_id: uuid.UUID) -> uuid.UUID:
    row = UsageLog.objects.create(
        tenant_id=tenant_id,
        category="llm",
        model="mock-chat",
        prompt_tokens=10,
        completion_tokens=5,
        request_id=str(uuid.uuid4()),
    )
    return uuid.UUID(str(row.id))


@pytest.fixture
def two_tenants_with_usage() -> Iterator[dict[str, uuid.UUID]]:
    created: dict[str, uuid.UUID] = {}
    for tenant_id, suffix in ((TENANT_A, "a"), (TENANT_B, "b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=f"tenant-{suffix}")
            created[suffix] = _usage_row(tenant_id)
    yield created


class TestPolicyIsDeclared:
    def test_the_table_has_rls_enabled_and_forced(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
                [TABLE],
            )
            enabled, forced = cursor.fetchone()

        assert enabled, f"{TABLE} 沒有啟用 RLS"
        assert forced, f"{TABLE} 沒有 FORCE——owner 連線會完全繞過 policy"

    def test_the_policy_has_both_using_and_with_check(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT qual, with_check FROM pg_policies "
                "WHERE tablename = %s AND policyname = 'tenant_isolation'",
                [TABLE],
            )
            row = cursor.fetchone()

        assert row is not None, f"{TABLE} 沒有 tenant_isolation policy"
        using, with_check = row
        assert using and "app.tenant_id" in using
        assert with_check and "app.tenant_id" in with_check

    def test_every_partition_forces_rls(self) -> None:
        """子分區是獨立的表，父表的 policy 管不到直接查它的人；且分區會被 Beat
        持續新增（理由詳 test_rls_conversation.py 的同名測試）。"""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_inherits i ON i.inhrelid = c.oid
                WHERE i.inhparent = %s::regclass
                """,
                [TABLE],
            )
            partitions = cursor.fetchall()

        assert partitions, "一個分區都沒有"
        unprotected = [name for name, enabled, forced in partitions if not (enabled and forced)]
        assert not unprotected, f"這些分區沒有 FORCE RLS：{unprotected}"


class TestIsolation:
    def test_each_tenant_only_sees_its_own_rows(
        self, two_tenants_with_usage: dict[str, uuid.UUID]
    ) -> None:
        with tenant_context(TENANT_A), unit_of_work():
            visible = _raw_ids()

        assert visible == {two_tenants_with_usage["a"]}

    def test_no_tenant_context_sees_nothing(
        self, two_tenants_with_usage: dict[str, uuid.UUID]
    ) -> None:
        assert two_tenants_with_usage  # 資料確實存在，空集合才有意義

        assert _raw_ids() == set()

    def test_cannot_insert_into_another_tenant(
        self, two_tenants_with_usage: dict[str, uuid.UUID]
    ) -> None:
        """`WITH CHECK` 那一半：以 A 的身分寫 B 的列必須被擋。"""
        from django.db.utils import ProgrammingError

        with (
            pytest.raises(ProgrammingError),
            tenant_context(TENANT_A),
            unit_of_work(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                f"INSERT INTO {TABLE} "  # noqa: S608
                "(id, tenant_id, category, model, prompt_tokens, completion_tokens, "
                " request_id, created_at) "
                "VALUES (%s, %s, 'llm', 'mock-chat', 1, 1, %s, now())",
                [str(uuid.uuid4()), str(TENANT_B), str(uuid.uuid4())],
            )

    def test_cannot_update_another_tenants_rows(
        self, two_tenants_with_usage: dict[str, uuid.UUID]
    ) -> None:
        """`USING` 管 UPDATE 的可見範圍：改不到就是 0 列，而不是錯誤——
        所以要驗「B 的列毫髮無傷」。"""
        with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
            cursor.execute(f"UPDATE {TABLE} SET prompt_tokens = 999999")  # noqa: S608

        with tenant_context(TENANT_B), unit_of_work():
            row = UsageLog.objects.get(id=two_tenants_with_usage["b"])

        assert row.prompt_tokens != 999999

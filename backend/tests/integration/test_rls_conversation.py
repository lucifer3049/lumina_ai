"""驗收：Conversation 三張表的 RLS（05 §5.1、13 §3 工作包 1D-1）。

方法論同 `test_rls_knowledge.py`，不重述：查詢一律繞過 Repository 直接下沒有
`WHERE tenant_id` 的原生 SQL（用 ORM 會同時經過 filter 與 policy，綠燈分不出是哪一道
擋的），四種寫入路徑分開驗（`USING` 管讀、`WITH CHECK` 管寫）。

**這一組的洩漏後果是三張表裡最嚴重的。** knowledge 洩漏的是文件內容——那是租戶
「放進系統」的東西；messages 洩漏的是**使用者問了什麼、系統答了什麼**，那是行為紀錄，
往往比文件本身更敏感（誰在查什麼案子、誰在問裁員規定）。而且對話是逐則累積的，
一次漏就是整段歷史。

**分區表的 RLS 有一個獨有的陷阱**：policy 建在父表上，對「經由父表的查詢」生效——
Django 一律經父表，所以正常路徑是安全的。但**新建的分區不會自動繼承 policy**，
直接查子分區時 policy 不適用。因此這裡除了查目錄，還逐一驗每個子分區的狀態
（`TestPartitionInheritance`），否則 2A 的 Celery Beat 建出新分區時會出現一個
「查父表安全、查子分區不安全」的缺口，而它完全無症狀。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from django.db import connection
from django.db.utils import ProgrammingError

from core.tenant import tenant_context
from core.uow import unit_of_work
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.conversation import make_conversation, make_memory_snapshot, make_message
from tests.factories.identity import make_tenant, make_user, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)

# **新表一律要加進這個清單**：漏掉的表不會有任何症狀——查詢照常回傳，只是範圍
# 變成整個資料庫（1C-2 的 embeddings 已經是同一個教訓）。
CONVERSATION_TABLES = (
    "conversation_conversation",
    "conversation_message",
    "conversation_memorysnapshot",
)


def _raw_ids(table: str) -> set[uuid.UUID]:
    """直接查，不帶任何租戶條件——看得到什麼完全由 policy 決定。"""
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT id FROM {table}")  # noqa: S608 —— 表名來自本檔常數
        return {row[0] for row in cursor.fetchall()}


@pytest.fixture
def two_tenants_with_conversations() -> Iterator[dict[str, uuid.UUID]]:
    """兩個租戶各一個對話、一則訊息、一份記憶摘要。"""
    created: dict[str, uuid.UUID] = {}
    for tenant_id, suffix in ((TENANT_A, "a"), (TENANT_B, "b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=f"tenant-{suffix}")
            user = make_user(tenant_id=tenant_id, email=f"owner@tenant-{suffix}.example")
            conversation = make_conversation(tenant_id=tenant_id, user_id=user.id)
            message = make_message(conversation=conversation, content=f"{suffix} 的訊息")
            snapshot = make_memory_snapshot(conversation=conversation, summary=f"{suffix} 的摘要")
            created[f"conversation_{suffix}"] = conversation.id
            created[f"message_{suffix}"] = message.id
            created[f"snapshot_{suffix}"] = snapshot.id
    yield created


class TestPolicyIsDeclared:
    @pytest.mark.parametrize("table", CONVERSATION_TABLES)
    def test_table_has_rls_enabled_and_forced(self, table: str) -> None:
        """`ENABLE` 之外還要 `FORCE`：owner 建的表對 owner 預設豁免 policy（13 §3.1）。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
                [table],
            )
            enabled, forced = cursor.fetchone()

        assert enabled, f"{table} 沒有啟用 RLS"
        assert forced, f"{table} 沒有 FORCE——owner 連線會完全繞過 policy"

    @pytest.mark.parametrize("table", CONVERSATION_TABLES)
    def test_policy_has_both_using_and_with_check(self, table: str) -> None:
        """只有 `USING` 的 policy 擋得住讀、擋不住寫進別的租戶名下，而讀取測試看不到。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT qual, with_check FROM pg_policies "
                "WHERE tablename = %s AND policyname = 'tenant_isolation'",
                [table],
            )
            row = cursor.fetchone()

        assert row is not None, f"{table} 沒有 tenant_isolation policy"
        using, with_check = row
        assert using and "app.tenant_id" in using
        assert with_check and "app.tenant_id" in with_check


class TestPartitionInheritance:
    def test_every_message_partition_forces_rls(self) -> None:
        """**每一個子分區都要自己開 RLS**。

        父表的 policy 只保護「經由父表」的存取。子分區是獨立的表，直接查它時父表的
        policy 不適用——而 `make psql-app` 或任何維運查詢都可能直接指到子分區。

        更重要的是它的時間性：2A 的 Celery Beat 會建出**新的**分區，而新分區不會自動
        繼承任何東西。這條測試逐一檢查現有的每一個分區，因此漏掉的那個一定會紅。
        """
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_inherits i ON i.inhrelid = c.oid
                JOIN pg_class p ON p.oid = i.inhparent
                WHERE p.relname = 'conversation_message'
            """)
            partitions = cursor.fetchall()

        assert partitions, "一個分區都沒有"
        unprotected = [name for name, enabled, forced in partitions if not (enabled and forced)]
        assert not unprotected, f"這些分區沒有 FORCE RLS：{unprotected}"


class TestSelectIsolation:
    @pytest.mark.parametrize(
        ("table", "own_key", "other_key"),
        [
            ("conversation_conversation", "conversation_a", "conversation_b"),
            ("conversation_message", "message_a", "message_b"),
            ("conversation_memorysnapshot", "snapshot_a", "snapshot_b"),
        ],
    )
    def test_each_tenant_only_sees_its_own_rows(
        self,
        two_tenants_with_conversations: dict[str, uuid.UUID],
        table: str,
        own_key: str,
        other_key: str,
    ) -> None:
        with tenant_context(TENANT_A), unit_of_work():
            visible = _raw_ids(table)

        assert visible == {two_tenants_with_conversations[own_key]}
        assert two_tenants_with_conversations[other_key] not in visible

    def test_no_tenant_context_sees_nothing(
        self, two_tenants_with_conversations: dict[str, uuid.UUID]
    ) -> None:
        """沒有交易區域參數時一列都看不到（fail closed）。"""
        assert two_tenants_with_conversations  # 資料確實存在，下面的空集合才有意義

        for table in CONVERSATION_TABLES:
            assert _raw_ids(table) == set(), f"{table} 在沒有租戶 context 時仍回傳資料"


class TestWriteIsolation:
    def test_cannot_insert_a_message_for_another_tenant(
        self, two_tenants_with_conversations: dict[str, uuid.UUID]
    ) -> None:
        """在租戶 A 的交易裡把訊息寫成租戶 B 的——`WITH CHECK` 必須擋下。

        擋不住的話，一個寫錯 tenant_id 的 bug 會把 A 的對話內容存進 B 的歷史裡，
        而 B 下次開啟那個對話就會看到不屬於他的問答。
        """
        with (
            tenant_context(TENANT_A),
            unit_of_work(),
            pytest.raises(ProgrammingError),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "INSERT INTO conversation_message "
                "(id, tenant_id, conversation_id, role, content, citations, tool_calls, "
                " tool_results, usage, status, created_at, updated_at) "
                "VALUES (%s, %s, %s, 'user', '越權', '[]', '[]', '[]', '{}', 'completed', "
                " now(), now())",
                [uuid.uuid4(), TENANT_B, two_tenants_with_conversations["conversation_b"]],
            )

    def test_cannot_update_another_tenants_messages(
        self, two_tenants_with_conversations: dict[str, uuid.UUID]
    ) -> None:
        with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE conversation_message SET content = '被竄改' WHERE id = %s",
                [two_tenants_with_conversations["message_b"]],
            )
            assert cursor.rowcount == 0

        with tenant_context(TENANT_B), unit_of_work(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT content FROM conversation_message WHERE id = %s",
                [two_tenants_with_conversations["message_b"]],
            )
            assert cursor.fetchone()[0] == "b 的訊息"

    def test_cannot_delete_another_tenants_conversations(
        self, two_tenants_with_conversations: dict[str, uuid.UUID]
    ) -> None:
        with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM conversation_conversation WHERE id = %s",
                [two_tenants_with_conversations["conversation_b"]],
            )
            assert cursor.rowcount == 0

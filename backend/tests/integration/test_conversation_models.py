"""驗收：Chat 資料層（05 §3.4／§4／§5.2、13 §3 工作包 1D-1）。

三張表撐起整個 1D：`conversations`（對話）、`messages`（每一則訊息，**按月分區**）、
`memory_snapshots`（記憶視窗的摘要，1D-5 才會寫入）。

放 integration 而不是 unit，因為要驗的東西全部是 DB 的性質：分區、約束、索引、RLS。
用假物件驗這一層等於什麼都沒驗。

**分區是這一包唯一不可逆的決定。** 普通表改成分區表要重建整張表，而 05 §5.2 把
`messages` 列為高成長表——愈晚改愈痛，且需要停機。因此 day-1 就建成分區表，即使現在
一列資料都還沒有。

分區帶來三個只會在特定時刻爆炸的失敗，本檔逐一釘死：

1. **分區鍵必須在 PK 裡**（PostgreSQL 的硬性要求）。漏了的話建表就失敗，但如果有人
   為了讓 migration 過而把 PK 改成只有 `created_at`，訊息 id 就不再唯一——而症狀要
   等到兩則訊息剛好同一微秒才出現。
2. **未來的分區要先建好**。沒有涵蓋當下時間的分區時，INSERT 直接失敗——而那發生在
   使用者按下送出的當下。05 §5.2 說由 Celery Beat 月初預建，**但 Beat 目前不存在**
   （排 2A），所以這裡改成 migration 一次預建 12 個月 + 一條會提前紅燈的測試。
3. **RLS 要對分區表生效**。policy 建在父表上，而 Django 一律經父表查詢——這件事必須
   用行為驗證（真的跨租戶查一次），而不是查目錄看 policy 在不在。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection

from apps.conversation.models import Message
from repositories.conversation import (
    ConversationRepository,
    MemorySnapshotRepository,
    MessageRepository,
)
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.conversation import make_conversation, make_memory_snapshot, make_message
from tests.factories.identity import make_tenant, make_user, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)

# 05 §5.2 說 Beat 月初預建「下 3 個月」。Beat 不存在期間，migration 預建 12 個月，
# 而剩餘不足 3 個月時這一包的測試要先紅——那是「該去建下一批了」的訊號，
# 而不是等到寫入失敗才發現。
MIN_MONTHS_AHEAD = 3


@pytest.fixture
def tenants() -> dict[uuid.UUID, uuid.UUID]:
    """兩個租戶，各一個使用者（conversations 需要 user_id）。"""
    users: dict[uuid.UUID, uuid.UUID] = {}
    for tenant_id, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=slug)
            users[tenant_id] = make_user(tenant_id=tenant_id, email=f"owner@{slug}.example").id
    return users


class TestPartitioning:
    def test_messages_is_a_partitioned_table(self) -> None:
        """``relkind = 'p'`` 才是分區表；``'r'`` 是普通表。

        建成普通表也能跑、測試也會綠——差別只在幾百萬列之後的維運成本，以及那時候
        要停機重建。這條測試是那個決定唯一的守門。
        """
        with connection.cursor() as cursor:
            cursor.execute("SELECT relkind FROM pg_class WHERE relname = 'conversation_message'")
            row = cursor.fetchone()

        assert row is not None, "conversation_message 不存在"
        assert row[0] == "p", f"不是分區表（relkind={row[0]}）"

    def test_it_is_partitioned_by_created_at_monthly(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_get_partkeydef(oid) FROM pg_class WHERE relname = 'conversation_message'"
            )
            partkey = cursor.fetchone()[0]

        assert partkey == "RANGE (created_at)", partkey

    def test_the_primary_key_includes_the_partition_key(self) -> None:
        """PostgreSQL 要求分區鍵必須是每個唯一約束的一部分（05 §3.4 已標明）。

        為了讓 migration 過而把 PK 改成只有 `created_at` 的話，訊息 id 就不再唯一
        ——而症狀要等到兩則訊息剛好落在同一個時間戳才出現。
        """
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT a.attname
                FROM pg_index i
                JOIN pg_class c ON c.oid = i.indrelid
                JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
                WHERE c.relname = 'conversation_message' AND i.indisprimary
                ORDER BY a.attname
            """)
            columns = [row[0] for row in cursor.fetchall()]

        assert set(columns) == {"id", "created_at"}, columns

    def test_enough_future_partitions_exist(self) -> None:
        """**沒有涵蓋當下時間的分區時，INSERT 直接失敗**——而那發生在使用者按送出的當下。

        這條測試會隨時間自然逼近門檻，那是刻意的：它是「該建下一批分區了」的提醒，
        而提醒出現在 CI 紅燈上，比出現在使用者的錯誤訊息上早好幾個月。

        Celery Beat 的自動預建屬 2A（見模組 docstring）。
        """
        horizon = datetime.now(UTC) + timedelta(days=30 * MIN_MONTHS_AHEAD)

        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT c.relname, pg_get_expr(c.relpartbound, c.oid)
                FROM pg_class c
                JOIN pg_inherits i ON i.inhrelid = c.oid
                JOIN pg_class p ON p.oid = i.inhparent
                WHERE p.relname = 'conversation_message'
            """)
            bounds = cursor.fetchall()

        assert bounds, "一個分區都沒有——所有寫入都會失敗"
        # 分區上界的字串形如 FOR VALUES FROM ('2026-08-01...') TO ('2026-09-01...')
        latest = max(bound.split("TO (")[1].strip("')") for _, bound in bounds)
        assert latest >= horizon.strftime("%Y-%m-%d"), (
            f"分區只建到 {latest}，不足 {MIN_MONTHS_AHEAD} 個月——請預建下一批"
        )

    def test_a_message_lands_in_the_partition_for_its_month(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        """寫進去的資料真的落在對應月份的子分區。

        分區建了但**範圍算錯**（例如全部指向同一個月）時，寫入照常成功、查詢照常
        回傳，只是分區完全沒有發揮作用——而那要到清理舊資料時才會發現：本來該
        `DROP` 一個分區的操作，變成刪不掉任何東西。
        """
        # 原生查詢也要在租戶 context 內：RLS 是 fail closed 的，context 外查會回空集合
        # 而不是報錯——第一次寫這條測試時就是這樣拿到 None 的。
        with tenant_scope(TENANT_A), connection.cursor() as cursor:
            conversation = make_conversation(tenant_id=TENANT_A, user_id=tenants[TENANT_A])
            message = make_message(conversation=conversation, role="user", content="哈囉")
            created_at = message.created_at

            cursor.execute(
                "SELECT tableoid::regclass::text FROM conversation_message WHERE id = %s",
                [message.id],
            )
            row = cursor.fetchone()

        assert row is not None, "在租戶 context 內仍查不到剛寫入的訊息"
        partition = row[0]

        assert (
            created_at.strftime("%Y_%m") in partition or created_at.strftime("%Y%m") in partition
        ), f"落在 {partition}，與建立時間 {created_at:%Y-%m} 對不上"


class TestSchema:
    def test_citations_round_trip_as_jsonb(self, tenants: dict[uuid.UUID, uuid.UUID]) -> None:
        """引用存 jsonb 而不是關聯表（05 §3.4：讀多寫一、無獨立查詢需求）。

        1D-5 會把它填滿，1E 靠它渲染引用面板。存成字串的話，前端要自己 parse，
        而「哪一版的格式」會變成一個沒有人記得的約定。
        """
        citations = [{"chunk_id": str(uuid.uuid4()), "doc_id": str(uuid.uuid4()), "score": 0.87}]

        with tenant_scope(TENANT_A):
            conversation = make_conversation(tenant_id=TENANT_A, user_id=tenants[TENANT_A])
            message = make_message(conversation=conversation, citations=citations)
            stored = Message.objects.get(id=message.id)

        assert stored.citations == citations
        assert stored.citations[0]["score"] == 0.87

    def test_a_message_records_the_generation_snapshot(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        """`model` 與 `prompt_version` 是**生成當下的快照**（05 §3.4：可追溯）。

        不存的話，換模型或改 prompt 之後就再也回答不了「這個答案當初是怎麼產生的」
        ——而那正是評測（Phase 3）與客訴處理唯一的依據。
        """
        with tenant_scope(TENANT_A):
            conversation = make_conversation(tenant_id=TENANT_A, user_id=tenants[TENANT_A])
            message = make_message(
                conversation=conversation,
                role="assistant",
                model="gemini-embedding-2",
                prompt_version=3,
                usage={"prompt_tokens": 120, "completion_tokens": 45, "ttft_ms": 480},
            )
            stored = Message.objects.get(id=message.id)

        assert stored.model == "gemini-embedding-2"
        assert stored.prompt_version == 3
        assert stored.usage["ttft_ms"] == 480

    def test_the_expected_indexes_exist(self) -> None:
        """05 §4 的三組索引。少了它們，對話載入與列表會隨資料量線性變慢，
        而功能完全正常——那是最晚被發現的一種退化。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename, indexdef FROM pg_indexes "
                "WHERE tablename IN ('conversation_conversation', 'conversation_message')"
            )
            rows = cursor.fetchall()
            definitions = " ".join(f"{table}:{definition}" for table, definition in rows)

        assert "conversation_id, created_at" in definitions, "缺對話載入用的索引"
        assert "tenant_id, created_at" in definitions, "缺租戶查詢用的索引"
        # 對話列表：只列未刪除的，依最後訊息時間排序（partial index）。
        assert "last_message_at DESC" in definitions, "缺對話列表用的索引"
        assert "deleted_at IS NULL" in definitions, "對話列表索引不是 partial"


class TestConversationRepository:
    def test_it_only_returns_this_tenants_conversations(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        with tenant_scope(TENANT_B):
            make_conversation(tenant_id=TENANT_B, user_id=tenants[TENANT_B], title="B 的對話")
        with tenant_scope(TENANT_A):
            make_conversation(tenant_id=TENANT_A, user_id=tenants[TENANT_A], title="A 的對話")

            titles = [c.title for c in ConversationRepository().list_for_tenant()]

        assert titles == ["A 的對話"]

    def test_a_soft_deleted_conversation_disappears(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        """對話是 05 §5.4 明列的軟刪除實體（使用者可能後悔）。

        只寫 `deleted_at` 卻沒從查詢排除的話，使用者會看到自己剛刪掉的對話還在列表上
        ——而刪除 API 回了 204。
        """
        with tenant_scope(TENANT_A):
            conversation = make_conversation(tenant_id=TENANT_A, user_id=tenants[TENANT_A])
            repository = ConversationRepository()

            repository.soft_delete(conversation.id)

            assert repository.get_by_id(conversation.id) is None
            assert repository.list_for_tenant() == []
            # 列還在，只是看不到——30 天後由清理 job 硬刪。
            assert repository.including_deleted().filter(id=conversation.id).exists()

    def test_message_counters_are_updated_in_one_place(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        """`message_count` 與 `last_message_at` 是反正規化欄位（05 §3.4、§6）。

        05 §6 說這是刻意的讀優化，前提是**寫入點單一**。散在多處的話，總有一條路徑
        忘了更新，而症狀是對話列表的排序與未讀數與實際內容不符——沒有錯誤訊息。
        """
        with tenant_scope(TENANT_A):
            conversation = make_conversation(tenant_id=TENANT_A, user_id=tenants[TENANT_A])
            messages = MessageRepository()
            conversations = ConversationRepository()

            first = messages.append(conversation_id=conversation.id, role="user", content="問題")
            messages.append(conversation_id=conversation.id, role="assistant", content="回答")

            refreshed = conversations.get_by_id(conversation.id)

        assert refreshed is not None
        assert refreshed.message_count == 2
        assert refreshed.last_message_at is not None
        assert refreshed.last_message_at >= first.created_at


class TestMessageRepository:
    def test_messages_come_back_in_chronological_order(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        """對話要照順序讀回來。

        沒有明確 ORDER BY 時 PostgreSQL 不保證順序——**分區表尤其如此**，因為它可能
        平行掃描多個分區再合併。症狀是跨月的對話讀回來時前後顛倒，而同月的對話完全
        正常，所以本機測不出來。
        """
        with tenant_scope(TENANT_A):
            conversation = make_conversation(tenant_id=TENANT_A, user_id=tenants[TENANT_A])
            repository = MessageRepository()
            for index in range(5):
                repository.append(
                    conversation_id=conversation.id, role="user", content=f"第 {index} 則"
                )

            contents = [m.content for m in repository.for_conversation(conversation.id)]

        assert contents == [f"第 {index} 則" for index in range(5)]

    def test_another_tenants_messages_are_invisible(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        with tenant_scope(TENANT_B):
            other = make_conversation(tenant_id=TENANT_B, user_id=tenants[TENANT_B])
            make_message(conversation=other, content="B 的秘密")

        with tenant_scope(TENANT_A):
            visible = MessageRepository().for_conversation(other.id)

        assert visible == []


class TestMemorySnapshotRepository:
    def test_the_latest_snapshot_wins(self, tenants: dict[uuid.UUID, uuid.UUID]) -> None:
        """記憶摘要有版本（05 §3.4 的 `version`）。1D-5 只讀最新的那一份。

        取錯版本的後果是 LLM 拿到過期的對話摘要——回答會參照到早已被修正的內容，
        而那看起來像模型在胡說。
        """
        with tenant_scope(TENANT_A):
            conversation = make_conversation(tenant_id=TENANT_A, user_id=tenants[TENANT_A])
            make_memory_snapshot(conversation=conversation, version=1, summary="舊摘要")
            make_memory_snapshot(conversation=conversation, version=2, summary="新摘要")

            latest = MemorySnapshotRepository().latest_for(conversation.id)

        assert latest is not None and latest.summary == "新摘要"

    def test_no_snapshot_yet_is_not_an_error(self, tenants: dict[uuid.UUID, uuid.UUID]) -> None:
        """新對話沒有摘要——那是正常狀態，不是例外（1D-5 的視窗版一開始就沒有）。"""
        with tenant_scope(TENANT_A):
            conversation = make_conversation(tenant_id=TENANT_A, user_id=tenants[TENANT_A])

            assert MemorySnapshotRepository().latest_for(conversation.id) is None

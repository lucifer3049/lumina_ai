"""驗收：notifications 資料層（05 §3.3、04 §8.5，13 §4 工作包 2A-5）。

通知是 2A 的最後一塊：DLQ 進了、quota 擋了、文件 ready 了——這些事實從 1B 起就
寫在 DB 裡，但**沒有任何一條路徑會主動告訴使用者**。使用者看得到的是「上傳完就
沒有下文」，而維運看得到的是一排 failed 文件沒有人來問。

這張表不分區（05 §5.2 點名的三張高成長表不含它）：一個租戶的通知量是「事件數」
而不是「請求數」，量級差三個數量級。

三件錯了都不會有例外：

1. **`dedupe_key` 沒有唯一約束**。去重寫在 service 裡「查一下、沒有才插」——
   兩個 worker 同時完成兩份文件時各查各的、各插一列，quota 的 80% 告警一個週期
   發兩次，而收件匣看起來只是「有點吵」。約束在 DB 上，第二個插入才會撞牆。
2. **RLS 沒開**。通知的標題就是內容摘要（「法規手冊.pdf 解析失敗」），跨租戶
   讀得到等於把對方的檔名與失敗原因端出去。查詢不會報錯，只會多回幾列。
3. **收件匣索引缺失**。鈴鐺每次開啟都要算未讀數，而未讀是「這個人的、還沒讀的」
   ——少了 (tenant_id, user_id, updated_at) 就是掃全表，症狀是通知愈多前端愈慢。

`updated_at` 是 05 §3.3 欄位表沒有的一欄（**待同步文件**）：收合（同一個時間桶
內的多份 ready 合成一列）必須有一個「最後一次有動靜」的時間可以排序，而改
`created_at` 等於竄改建立時間——收件匣會出現「三分鐘前建立」的三小時前那一列。
"""

from __future__ import annotations

import uuid
from typing import cast

import pytest
from django.db import IntegrityError, connection, transaction
from services.platform.notifications import TYPE_DOCUMENT_READY, TYPE_QUOTA_THRESHOLD

from apps.platform.models import Notification
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.platform import NotificationRepository
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)

TABLE = "platform_notification"
USER = uuid.UUID("aaaaaaaa-0000-5000-8000-00000000000a")
OTHER_USER = uuid.UUID("bbbbbbbb-0000-5000-8000-00000000000b")


@pytest.fixture(autouse=True)
def _tenants() -> None:
    for tenant_id in (TENANT_A, TENANT_B):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id)


def _columns() -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = %s",
            [TABLE],
        )
        return {str(name): str(kind) for name, kind in cursor.fetchall()}


def _indexes() -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", [TABLE])
        return {str(name): str(definition) for name, definition in cursor.fetchall()}


def _row(tenant_id: uuid.UUID, **overrides: object) -> Notification:
    fields: dict[str, object] = {
        "tenant_id": tenant_id,
        "user_id": USER,
        "type": TYPE_DOCUMENT_READY,
        "title": "法規手冊.pdf 已完成",
        "body": "可以開始問答了。",
        "channels": ["in_app"],
        "meta": {"document_id": str(uuid.uuid4())},
        "dedupe_key": None,
    }
    fields.update(overrides)
    with tenant_scope(tenant_id):
        return cast("Notification", Notification.objects.create(**fields))


class TestSchema:
    def test_the_documented_columns_exist(self) -> None:
        """05 §3.3 的欄位表 + `updated_at`（見模組 docstring）。"""
        columns = _columns()

        assert set(columns) >= {
            "id",
            "tenant_id",
            "user_id",
            "type",
            "title",
            "body",
            "channels",
            "read_at",
            "meta",
            "dedupe_key",
            "created_at",
            "updated_at",
        }

    def test_channels_is_an_array_not_a_json_blob(self) -> None:
        """05 §3.3 寫的是 `t[]`。用 jsonb 存陣列查得到但索引不了，而
        「哪些通知寄了 email」是寄信失敗時第一個要查的東西。"""
        assert _columns()["channels"] == "ARRAY"

    def test_the_inbox_index_exists(self) -> None:
        definition = _indexes().get("ix_notification_inbox", "")

        assert "tenant_id" in definition
        assert "user_id" in definition
        assert "updated_at" in definition

    def test_a_dedupe_key_may_not_repeat_for_the_same_recipient(self) -> None:
        """quota 的 80% 一個週期只該有一列——併發時擋在 DB 上，不是靠 service 先查。"""
        key = f"{TYPE_QUOTA_THRESHOLD}:tokens_month:2026-08-01:80"
        _row(TENANT_A, type=TYPE_QUOTA_THRESHOLD, dedupe_key=key)

        with pytest.raises(IntegrityError), transaction.atomic():
            _row(TENANT_A, type=TYPE_QUOTA_THRESHOLD, dedupe_key=key)

    def test_the_same_dedupe_key_may_exist_for_another_recipient(self) -> None:
        """owner 與 admin 各收到一份同樣的 quota 告警——去重是「每人一次」。"""
        key = f"{TYPE_QUOTA_THRESHOLD}:tokens_month:2026-08-01:80"
        _row(TENANT_A, dedupe_key=key)

        assert _row(TENANT_A, user_id=OTHER_USER, dedupe_key=key).id is not None

    def test_rows_without_a_dedupe_key_may_repeat(self) -> None:
        """`document.failed` 不去重：每一次失敗都是一件要處理的事。
        唯一約束若沒有排除 NULL，第二次失敗會被 DB 擋掉而使用者永遠不知道。"""
        _row(TENANT_A)

        assert _row(TENANT_A).id is not None


class TestRepository:
    def test_a_new_notification_lands_unread(self) -> None:
        with tenant_context(TENANT_A), unit_of_work():
            created = NotificationRepository().create(
                user_id=USER,
                type=TYPE_DOCUMENT_READY,
                title="法規手冊.pdf 已完成",
                body="可以開始問答了。",
                meta={},
                channels=["in_app"],
            )

        assert created.read_at is None

    def test_collapsing_updates_the_existing_row_instead_of_inserting(self) -> None:
        """一次上傳 50 份就是 50 列——收合把同一個時間桶內的合成一列
        （04 §8.5 的「去重與節流」）。"""
        key = f"{TYPE_DOCUMENT_READY}:{uuid.uuid4()}:0"
        with tenant_context(TENANT_A), unit_of_work():
            repository = NotificationRepository()
            first = repository.create(
                user_id=USER,
                type=TYPE_DOCUMENT_READY,
                title="1 份文件已完成",
                body="法規手冊.pdf",
                meta={"count": 1},
                channels=["in_app"],
                dedupe_key=key,
            )
            repository.collapse(
                first.id, title="2 份文件已完成", body="法規手冊.pdf 等 2 份", meta={"count": 2}
            )
            rows, _ = repository.inbox(user_id=USER, limit=10)

        assert [row.title for row in rows] == ["2 份文件已完成"]

    def test_collapsing_makes_a_read_notification_unread_again(self) -> None:
        """讀過之後又有新的一份完成——那是新的一件事，不是舊的那一件。"""
        key = f"{TYPE_DOCUMENT_READY}:{uuid.uuid4()}:0"
        with tenant_context(TENANT_A), unit_of_work():
            repository = NotificationRepository()
            row = repository.create(
                user_id=USER,
                type=TYPE_DOCUMENT_READY,
                title="1 份文件已完成",
                body="法規手冊.pdf",
                meta={"count": 1},
                channels=["in_app"],
                dedupe_key=key,
            )
            repository.mark_read(user_id=USER, notification_id=row.id)
            repository.collapse(row.id, title="2 份文件已完成", body="…", meta={"count": 2})

            assert repository.unread_count(USER) == 1

    def test_the_dedupe_lookup_finds_the_row_to_collapse_into(self) -> None:
        key = f"{TYPE_DOCUMENT_READY}:{uuid.uuid4()}:0"
        with tenant_context(TENANT_A), unit_of_work():
            repository = NotificationRepository()
            created = repository.create(
                user_id=USER,
                type=TYPE_DOCUMENT_READY,
                title="1 份文件已完成",
                body="法規手冊.pdf",
                meta={"count": 1},
                channels=["in_app"],
                dedupe_key=key,
            )

            found = repository.get_by_dedupe(user_id=USER, dedupe_key=key)

        assert found is not None
        assert found.id == created.id

    def test_the_inbox_is_newest_first_and_pages_without_losing_a_row(self) -> None:
        """游標分頁的形狀同對話與稽核（`repositories/base.py` 的 `split_page`）：
        多取一筆判斷有沒有下一頁，鍵是 (updated_at, id)——時間戳會撞，只用時間
        當游標會讓某一列在翻頁時消失。"""
        with tenant_context(TENANT_A), unit_of_work():
            repository = NotificationRepository()
            for index in range(5):
                repository.create(
                    user_id=USER,
                    type=TYPE_DOCUMENT_READY,
                    title=f"第 {index} 則",
                    body="",
                    meta={},
                    channels=["in_app"],
                )

            first_page, cursor = repository.inbox(user_id=USER, limit=3)
            assert cursor is not None
            second_page, end = repository.inbox(user_id=USER, limit=3, cursor=cursor)

        titles = [row.title for row in first_page] + [row.title for row in second_page]
        assert titles == [f"第 {index} 則" for index in (4, 3, 2, 1, 0)]
        assert end is None

    def test_unread_only_hides_what_has_been_read(self) -> None:
        with tenant_context(TENANT_A), unit_of_work():
            repository = NotificationRepository()
            read = repository.create(
                user_id=USER,
                type=TYPE_DOCUMENT_READY,
                title="讀過的",
                body="",
                meta={},
                channels=["in_app"],
            )
            repository.create(
                user_id=USER,
                type=TYPE_DOCUMENT_READY,
                title="沒讀的",
                body="",
                meta={},
                channels=["in_app"],
            )
            repository.mark_read(user_id=USER, notification_id=read.id)

            rows, _ = repository.inbox(user_id=USER, limit=10, unread_only=True)

        assert [row.title for row in rows] == ["沒讀的"]

    def test_one_persons_inbox_is_not_anothers(self) -> None:
        """同一個租戶裡，admin 看不到 owner 的收件匣——通知是**寄給某個人**的，
        不是租戶的公佈欄。"""
        with tenant_context(TENANT_A), unit_of_work():
            repository = NotificationRepository()
            repository.create(
                user_id=OTHER_USER,
                type=TYPE_DOCUMENT_READY,
                title="別人的",
                body="",
                meta={},
                channels=["in_app"],
            )

            rows, _ = repository.inbox(user_id=USER, limit=10)
            assert rows == []
            assert repository.unread_count(USER) == 0

    def test_marking_someone_elses_notification_read_does_nothing(self) -> None:
        """回 False 而不是拋例外：端點要把它轉成 404（存在與否本身就是資訊）。"""
        row = _row(TENANT_A, user_id=OTHER_USER)

        with tenant_context(TENANT_A), unit_of_work():
            assert NotificationRepository().mark_read(user_id=USER, notification_id=row.id) is False

    def test_marking_read_twice_keeps_the_first_timestamp(self) -> None:
        """重複點擊、多開分頁——第二次不該把「什麼時候讀的」改掉。"""
        row = _row(TENANT_A)
        with tenant_context(TENANT_A), unit_of_work():
            repository = NotificationRepository()
            repository.mark_read(user_id=USER, notification_id=row.id)
            first = repository.inbox(user_id=USER, limit=1)[0][0].read_at
            repository.mark_read(user_id=USER, notification_id=row.id)
            second = repository.inbox(user_id=USER, limit=1)[0][0].read_at

        assert first == second


class TestIsolation:
    def test_another_tenants_notifications_are_invisible(self) -> None:
        """RLS 是最後一道（05 §5.1）：原生 SQL 繞過 Repository 的 filter，
        policy 沒開的話這裡會看到兩列。"""
        _row(TENANT_A, title="A 的通知")
        _row(TENANT_B, title="B 的通知")

        with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
            cursor.execute(f"SELECT title FROM {TABLE}")  # noqa: S608 —— 表名是本檔常數
            titles = {str(row[0]) for row in cursor.fetchall()}

        assert titles == {"A 的通知"}

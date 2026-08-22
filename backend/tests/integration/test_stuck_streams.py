"""驗收：卡在 `streaming` 的訊息的補償掃描（05 §3.4 狀態機的縫隙）。

`ChatService` 只收得了尾**優雅的那一種**：關機時 `drain()` 送出 `CancelledError`，
那條路徑 shield 住收尾、把訊息標成 `interrupted` 並保住已產生的部分。

**硬殺沒有那條路徑。** OOM、`kill -9`、機器沒了——行程消失的那一刻那一列還是
`streaming`，而且再也沒有人會去動它：生成不是 Celery task（沒有 acks_late 把它還回
佇列），也沒有任何排程掃描它。使用者看到的是永遠停在「正在輸入」的一則訊息，重新
整理也一樣，因為那就是資料庫裡的狀態。文件那邊早有這件事，訊息這邊沒有。

三件事錯了都不會有例外：

1. **門檻太短**——還在跑的長回答被標成中斷，然後那一輪結束時又寫回 completed，
   使用者看到訊息「先中斷再完成」。
2. **清掉了 content**——09 附錄 A 對 `STREAM_INTERRUPTED` 的要求是「partial 已保存」，
   而使用者留在畫面上的正是那半句話。
3. **漏租戶**（同其他逐租戶迴圈的風險）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from apps.conversation.models import Message
from services.conversation.rescue import StuckStreamRescueService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.conversation import make_conversation, make_message
from tests.factories.identity import make_tenant, make_user, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)

_LONG_AGO = datetime.now(UTC) - timedelta(hours=2)


@pytest.fixture
def tenants() -> dict[uuid.UUID, uuid.UUID]:
    """兩個租戶各一個使用者——對話要有 owner，而 `user_id` 一定要顯式帶
    （不帶的話 factory 的 SubFactory 會在這個租戶的 context 裡再造一個租戶，
    被 `identity_tenant` 的 RLS 擋下，見 tests/factories/conversation.py）。"""
    people = {}
    for tenant_id, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=slug)
            people[tenant_id] = uuid.UUID(str(make_user(tenant_id=tenant_id).id))
    return people


def _message(
    tenant_id: uuid.UUID,
    people: dict[uuid.UUID, uuid.UUID],
    *,
    status: str,
    stale: bool = True,
    content: str = "答到一半",
) -> uuid.UUID:
    with tenant_scope(tenant_id):
        conversation = make_conversation(tenant_id=tenant_id, user_id=people[tenant_id])
        message = make_message(
            conversation=conversation, role="assistant", status=status, content=content
        )
        if stale:
            # `created_at` 是生成開始的時刻，也是分區鍵；auto_now_add 蓋不掉建立值，
            # 用 queryset update 把它推回過去。
            Message.objects.filter(id=message.id).update(created_at=_LONG_AGO)
        return uuid.UUID(str(message.id))


def _reload(tenant_id: uuid.UUID, message_id: uuid.UUID) -> Message:
    with tenant_scope(tenant_id):
        return Message.objects.get(id=message_id)


class TestRescue:
    def test_a_long_dead_stream_is_marked_interrupted(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        """**這是原本沒有人收的那個尾。** 產生它的行程已經不在了，而那一列還在
        `streaming`——前端看到的是永遠不會結束的「正在輸入」。"""
        message_id = _message(TENANT_A, tenants, status="streaming")

        assert StuckStreamRescueService().rescue_tenant(TENANT_A) == 1

        assert _reload(TENANT_A, message_id).status == "interrupted"

    def test_the_partial_answer_is_kept(self, tenants: dict[uuid.UUID, uuid.UUID]) -> None:
        """09 附錄 A：`STREAM_INTERRUPTED` 要求 partial 已保存。清成空字串的話，
        使用者畫面上那半句話會在重新整理之後消失——比停在「正在輸入」更難解釋。"""
        message_id = _message(TENANT_A, tenants, status="streaming", content="第一段已經產生")

        StuckStreamRescueService().rescue_tenant(TENANT_A)

        assert _reload(TENANT_A, message_id).content == "第一段已經產生"

    def test_it_records_why(self, tenants: dict[uuid.UUID, uuid.UUID]) -> None:
        """`error.code` 與關機那條路徑相同（對使用者是同一件事），`cause` 分得出
        是誰收的尾——那是排查時唯一有用的差別。"""
        message_id = _message(TENANT_A, tenants, status="streaming")

        StuckStreamRescueService().rescue_tenant(TENANT_A)

        error = _reload(TENANT_A, message_id).error
        assert error is not None
        assert error["code"] == "STREAM_INTERRUPTED"
        assert error["cause"] == "worker_lost"


class TestBoundaries:
    def test_a_stream_that_just_started_is_left_alone(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        """門檻擋的是「正常也會待一陣子」：生成本身有 120 秒的牆鐘上限，標得太早
        會讓訊息先中斷再完成。"""
        message_id = _message(TENANT_A, tenants, status="streaming", stale=False)

        assert StuckStreamRescueService().rescue_tenant(TENANT_A) == 0

        assert _reload(TENANT_A, message_id).status == "streaming"

    @pytest.mark.parametrize("status", ["completed", "interrupted", "failed"])
    def test_finished_messages_are_not_touched(
        self, tenants: dict[uuid.UUID, uuid.UUID], status: str
    ) -> None:
        """終局狀態不得被改寫——尤其 `completed`：那會讓一則好好的回答變成中斷。"""
        message_id = _message(TENANT_A, tenants, status=status)

        StuckStreamRescueService().rescue_tenant(TENANT_A)

        assert _reload(TENANT_A, message_id).status == status

    def test_another_tenants_message_is_not_touched(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        """逐租戶掃描：RLS 之外，這裡自己也要證明沒有越界。"""
        theirs = _message(TENANT_B, tenants, status="streaming")

        StuckStreamRescueService().rescue_tenant(TENANT_A)

        assert _reload(TENANT_B, theirs).status == "streaming"

    def test_rescue_all_covers_every_active_tenant(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        """漏租戶的症狀是「有些人的訊息會自己收尾、有些人的永遠掛著」，
        而那與租戶本身沒有任何可見的關聯。"""
        _message(TENANT_A, tenants, status="streaming")
        _message(TENANT_B, tenants, status="streaming")

        assert StuckStreamRescueService().rescue_all() == 2

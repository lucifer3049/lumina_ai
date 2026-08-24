"""Conversation 的 factory（apps/conversation/models.py）。

形狀與 `tests/factories/knowledge.py` 一致：factory 類別關在模組內，對外只給有回傳
型別的 `make_*` 薄包裝。

**建立資料一律要在租戶 context ＋ 交易內**（沿用 identity 的 `tenant_scope`）：三張表
都有 RLS，`WITH CHECK` 讀的是交易區域參數 `app.tenant_id`。

`Message` 的 `created_at` 是**分區鍵**，因此 factory 不指定它——讓 DB 的 `auto_now_add`
決定，資料才會落在「現在」所屬的那個分區。宣告一個固定值會讓所有測試訊息擠進同一個
分區（分區形同虛設，而寫入與查詢照常成功）；宣告一個隨機值則會在抽到預建範圍外的
月份時變成隨機紅燈。

**要測跨月行為時不能傳 `created_at`**：它是 `auto_now_add`，Django 在 INSERT 時一律
覆寫，傳進去的值不會生效、也不會報錯——照做的測試會全部寫進當月然後通過，看起來
在驗跨月而一次都沒跨過。正確做法是建立之後走 `QuerySet.update()`（不經 `save()`，
`auto_now_add` 管不到；PostgreSQL ≥ 11 會把列搬到正確的分區）：

    message = make_message(conversation=conversation)
    Message.objects.filter(id=message.id).update(created_at=下個月某天)

目標月份必須有分區存在，而**預建只往未來**（migration 預建 12 個月，Beat 每月補到
第 3 個月）——過去的月份一個分區都沒有，寫過去會拿到
``no partition of relation "conversation_message" found for row``。

以上四件事由 `tests/integration/test_conversation_models.py::TestFactoryRespectsThePartitionKey`
釘住。
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import factory

from apps.conversation.models import Conversation, MemorySnapshot, Message


class ConversationFactory(factory.django.DjangoModelFactory[Conversation]):
    class Meta:
        model = Conversation

    id = factory.LazyFunction(uuid.uuid4)
    tenant = factory.SubFactory("tests.factories.identity.TenantFactory")
    user = factory.SubFactory("tests.factories.identity.UserFactory")
    title = factory.Sequence(lambda n: f"對話 {n}")
    kb_ids: list[uuid.UUID] = []
    prompt_key = ""
    status = "active"
    pinned = False
    message_count = 0
    last_message_at = None


class MessageFactory(factory.django.DjangoModelFactory[Message]):
    class Meta:
        model = Message

    id = factory.LazyFunction(uuid.uuid4)
    # tenant 跟著 conversation 走：兩者不一致的列會被 RLS 的 WITH CHECK 擋下，
    # 而錯誤訊息只說「violates row-level security policy」，看不出是哪一欄不對。
    tenant = factory.SelfAttribute("conversation.tenant")
    conversation = factory.SubFactory(ConversationFactory)
    role = "user"
    content = factory.Sequence(lambda n: f"訊息 {n}")
    citations: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    model = ""
    prompt_version = None
    usage: dict[str, Any] = {}
    status = "completed"


class MemorySnapshotFactory(factory.django.DjangoModelFactory[MemorySnapshot]):
    class Meta:
        model = MemorySnapshot

    id = factory.LazyFunction(uuid.uuid4)
    tenant = factory.SelfAttribute("conversation.tenant")
    conversation = factory.SubFactory(ConversationFactory)
    summary = factory.Sequence(lambda n: f"摘要 {n}")
    token_count = 0
    version = 1


def _resolve(kwargs: dict[str, Any]) -> dict[str, Any]:
    """``tenant_id=`` / ``user_id=`` 轉成物件（呼叫端多半只有 id）。

    **`user_id` 一定要轉**：不轉的話 `ConversationFactory.user` 的 SubFactory 會照跑，
    而 `UserFactory` 自己又帶一個 `TenantFactory`——於是在租戶 A 的 context 裡建出一個
    新租戶，被 `identity_tenant` 的 RLS `WITH CHECK` 擋下。錯誤訊息是
    「new row violates row-level security policy for table "identity_tenant"」，
    指向 identity 而不是這裡，第一次看到時完全想不到是 factory 的問題。
    """
    from apps.identity.models import Tenant, User

    if "tenant_id" in kwargs and "tenant" not in kwargs:
        kwargs["tenant"] = Tenant.objects.get(id=kwargs.pop("tenant_id"))
    if "user_id" in kwargs and "user" not in kwargs:
        kwargs["user"] = User.objects.get(id=kwargs.pop("user_id"))
    return kwargs


def make_conversation(**kwargs: Any) -> Conversation:
    return cast(Conversation, ConversationFactory(**_resolve(kwargs)))


def make_message(**kwargs: Any) -> Message:
    return cast(Message, MessageFactory(**_resolve(kwargs)))


def make_memory_snapshot(**kwargs: Any) -> MemorySnapshot:
    return cast(MemorySnapshot, MemorySnapshotFactory(**_resolve(kwargs)))

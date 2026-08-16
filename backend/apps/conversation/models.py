"""Conversation 的資料模型（05 §3.4）。

**Model 保持薄**（鐵則 6）：只有欄位、Meta、`__str__`。業務規則在 Service、查詢在
Repository。

三張表：`Conversation`（一場對話）、`Message`（每一則發言，**按月分區**）、
`MemorySnapshot`（對話太長時的摘要，1D-5 才會寫入）。

**`Message` 的實際 DDL 不由 Django 產生**（見 `migrations/0001_initial.py`）：Django
不支援分區表，因此那張表走 `SeparateDatabaseAndState`——Django 的 state 認得這個
model（ORM 照常運作），而真正的 `CREATE TABLE ... PARTITION BY RANGE` 由 migration
自己下。兩邊的欄位必須逐一對齊，對不上的症狀是 ORM 查詢報「欄位不存在」。
"""

from __future__ import annotations

import uuid

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models.functions import Coalesce

from apps.identity.models import Tenant, User


class TimestampedModel(models.Model):
    """標配三欄（05 §3 前言）：建立、更新、軟刪除時間。

    **這是第三份**（identity、knowledge 各有一份，內容相同）。knowledge 那份的註解
    寫明「共用的正確位置是一個共同的基礎模組，那需要動 identity」——而 identity 至今
    仍是各工作包的禁區，於是複本又多了一個。

    三份就該處理了：建議排一個小工作包把它抽到 `apps/common/`（1D-1 回報清單）。
    在那之前保持三份內容完全一致——不一致的話，`deleted_at` 的語意會依表而異，而
    軟刪除的查詢排除是寫在 Repository 基底裡的，它假設所有表長得一樣。
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class Conversation(TimestampedModel):
    """一場對話。軟刪除實體（05 §5.4：使用者可能後悔）。"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="conversations")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="conversations")
    title = models.TextField(default="", blank=True)
    # 這場對話檢索哪些知識庫。陣列而非關聯表：讀多寫一、且沒有「反查哪些對話用了這個
    # KB」的需求（同 05 §3.4 對 citations 的判斷）。
    kb_ids = ArrayField(models.UUIDField(), default=list, blank=True)
    model_config_id = models.UUIDField(null=True, blank=True)
    prompt_key = models.TextField(default="", blank=True)
    status = models.TextField(default="active")
    pinned = models.BooleanField(default=False)
    # 反正規化（05 §3.4、§6）：對話列表要依最後訊息時間排序並顯示則數，而那是列表
    # 頁最頻繁的查詢。**寫入點必須單一**（`MessageRepository.append`），散開就會漂。
    message_count = models.IntegerField(default=0)
    last_message_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "conversation_conversation"
        indexes = [
            # 05 §4：對話列表。三件事必須與 `ConversationRepository.list_for_tenant`
            # 的 ORDER BY **逐字對應**，否則索引建了也用不到：
            #
            # 1. partial（只含未刪除的）——列表永遠只看未刪除的，而軟刪除的資料會
            #    累積 30 天。
            # 2. 排序鍵是 `COALESCE(last_message_at, created_at)` 而不是
            #    `last_message_at`。**1D-1 原本寫後者，那是錯的**：`last_message_at`
            #    可為 NULL（剛建立、還沒發言），而列表要的是 NULLS LAST（空對話排
            #    後面）——PostgreSQL 的 DESC 預設卻是 NULLS FIRST，兩者對不上時
            #    planner 會放棄這個索引改做排序，而結果完全正確。
            # 3. COALESCE 同時讓游標只需要一個排序鍵（見 common/cursors.py 的呼叫端）
            #    ——三段式的 NULL 比較寫得出來，但每一段都是一次出錯的機會。
            models.Index(
                models.F("tenant"),
                models.F("user"),
                Coalesce("last_message_at", "created_at").desc(),
                condition=models.Q(deleted_at__isnull=True),
                name="ix_conv_tenant_user_recent",
            ),
        ]

    def __str__(self) -> str:
        return f"Conversation({self.id}: {self.title[:20]})"


class Message(TimestampedModel):
    """一則發言。**按月分區**（05 §3.4、§5.2）。

    分區的理由是刪除成本：訊息是高成長表，而保留政策到期時「DROP 一個分區」是一次
    metadata 操作，「DELETE 幾百萬列」則會產生同樣數量的死列，讓 autovacuum 追著跑
    好幾個小時，期間查詢一起變慢。

    **PK 是 `(id, created_at)`**：PostgreSQL 要求分區鍵必須在每個唯一約束裡。Django
    只認得單欄 PK，因此這裡宣告 `id`，而複合 PK 由 migration 的 DDL 建立——Django 的
    寫入一律以 `id` 定位，兩者不衝突。05 §6 已把「PK 含 created_at 讓 FK 引用不便」
    記為技術債，而 citations 用 jsonb 正好迴避了它。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="messages")
    conversation = models.ForeignKey(
        Conversation, on_delete=models.PROTECT, related_name="messages"
    )
    role = models.TextField()
    content = models.TextField(default="", blank=True)
    # 引用存 jsonb 而不是關聯表（05 §3.4：讀多寫一、無獨立查詢需求）。
    citations = models.JSONField(default=list, blank=True)
    tool_calls = models.JSONField(default=list, blank=True)
    tool_results = models.JSONField(default=list, blank=True)
    # 生成當下的快照（05 §3.4）。換模型或改 prompt 之後，這是唯一能回答「這個答案
    # 當初怎麼產生的」的依據——評測與客訴處理都靠它。
    model = models.TextField(default="", blank=True)
    prompt_version = models.IntegerField(null=True, blank=True)
    usage = models.JSONField(default=dict, blank=True)
    status = models.TextField(default="completed")
    error = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "conversation_message"
        # **索引不在這裡宣告**：分區表的索引要建在父表上才會傳播到每個分區，而那段
        # DDL 在 migration 裡（見 `0001_initial._create_message`）。在 Meta 宣告的話，
        # Django 的 state 會以為自己管理它們，下一次 makemigrations 就會產生一個
        # 對分區表跑不起來的 AddIndex。
        #
        # 不寫 `indexes = []` 是因為那會被 mypy 判成覆寫基底的 class variable
        # （django-stubs 的 TypedModelMeta）——留空即繼承預設的空清單，語意相同。

    def __str__(self) -> str:
        return f"Message({self.id}: {self.role})"


class MemorySnapshot(TimestampedModel):
    """對話記憶的摘要（05 §3.4、06 §5）。

    1D-5 的視窗版只讀最新的一份；背景摘要屬 Phase 3。`version` 遞增而不是覆寫，
    是為了讓「這次回答依據的是哪一版摘要」可追溯——摘要本身是 LLM 產出的，會錯。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="memory_snapshots")
    conversation = models.ForeignKey(
        Conversation, on_delete=models.PROTECT, related_name="memory_snapshots"
    )
    # 這份摘要涵蓋到哪一則訊息為止。**不是 FK**：messages 的 PK 含 created_at，
    # 單欄 FK 引用不過去（05 §6 記錄的技術債）。
    upto_message_id = models.UUIDField(null=True, blank=True)
    summary = models.TextField(default="", blank=True)
    token_count = models.IntegerField(default=0)
    version = models.IntegerField(default=1)

    class Meta:
        db_table = "conversation_memorysnapshot"
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "version"],
                name="uq_memory_conversation_version",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "conversation", "-version"],
                name="ix_memory_tenant_conv_ver",
            ),
        ]

    def __str__(self) -> str:
        return f"MemorySnapshot({self.conversation_id} v{self.version})"

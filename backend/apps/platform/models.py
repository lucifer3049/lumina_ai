"""Platform context 的資料模型（05 §3.3／§3.5：UsageLog、AuditLog、Notification…）。

**Model 保持薄**（鐵則 6）：只有欄位、Meta、`__str__`。

`UsageLog` 是 2A-1 進來的第一張表；`AuditLog` 於 2A-4 跟上；Notification 排 2A-5。
Phase 0 起這個 app 就存在，當時只承載 extension 的 migration（0001_extensions.py）。
"""

from __future__ import annotations

import uuid

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone

from apps.identity.models import Tenant


class UsageLog(models.Model):
    """一次計費消費（05 §3.3）。**按月分區、append-only**。

    分區的理由同 `Message`（05 §5.2 的高成長表）：保留政策 13 個月（05 §7），
    到期「DROP 一個分區」是 metadata 操作，DELETE 幾百萬列則讓 autovacuum 追著跑。
    實際 DDL 由 migration 的 `SeparateDatabaseAndState` 下（分區表 Django 不支援；
    模式同 `conversation/migrations/0001_initial.py`），**PK 實際是 `(id, created_at)`**。

    append-only，因此沒有 updated_at／deleted_at：帳不改、不刪，只到期整分區歸檔。

    `user_id`／`conversation_id` 是裸 UUID 不是 FK：使用者可被匿名化刪除（10 §資料
    保留）、對話可被清理，而**帳要活得比它們久**——FK 會讓刪除反過來被帳擋住。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="usage_logs")
    user_id = models.UUIDField(null=True, blank=True)
    # llm / embedding；storage（2A-2）、rerank（2B）之後加入。
    category = models.TextField()
    model = models.TextField()
    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    # None = 還不知道（缺價目），不是 0（免費）——見 services/platform/pricing.py。
    cost = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    conversation_id = models.UUIDField(null=True, blank=True)
    # 對映得回觸發它的那一次呼叫：chat 是 message_id、embedding 是文件與版本。
    request_id = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "platform_usagelog"
        # 索引不在這裡宣告——分區表的索引建在父表的 DDL 上（理由詳 `Message.Meta`）。

    def __str__(self) -> str:
        return f"UsageLog({self.category}: {self.model})"


class QuotaCounter(models.Model):
    """quota 的對帳快照（05 §3.3；2A-2b）。

    即時計數在 Redis，這張表是它的耐久影子：日結對帳把事實來源算出來的用量落地。
    **普通表、不分區**：一天每租戶最多幾列（資源 × 期別），成長率與 usage_logs
    差三個數量級。

    `(tenant, resource, period, period_start)` 唯一——對帳重跑是 upsert，
    同一期永遠只有一列。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="quota_counters")
    resource = models.TextField()
    # day / month。存量資源（documents、storage_bytes）快照為 day。
    period = models.TextField()
    period_start = models.DateField()
    used = models.BigIntegerField(default=0)
    # None＝當時不限制（存 0 會把「不限制」讀成「禁止」）。
    limit = models.BigIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "platform_quotacounter"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "resource", "period", "period_start"],
                name="uq_quotacounter_period",
            )
        ]

    def __str__(self) -> str:
        return f"QuotaCounter({self.resource} {self.period_start})"


class UsageDaily(models.Model):
    """usage 的日彙總（04 §8.2，2A-3）——`/analytics/*` 的資料源。

    每小時由 rollup 從 usage_logs 重算 upsert（同一格永遠一列）。普通表：
    一天每租戶最多「使用者 × 類別 × 模型」列，比 usage_logs 小三個數量級。

    **維度缺 kb**（04 §8.2 寫 per-kb）：usage_logs 沒有 kb_id（chat 是跨 KB 的
    對話），記為缺口、3B 評測需要時補欄位（2A-3 開工前人類核可的偏離）。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="usage_daily")
    day = models.DateField()
    # NULL＝系統行為（embedding）。唯一約束以 NULLS NOT DISTINCT 建（見 migration）
    # ——否則同一格的 NULL 列可以無限重複，upsert 形同虛設。
    user_id = models.UUIDField(null=True, blank=True)
    category = models.TextField()
    model = models.TextField()
    requests = models.IntegerField(default=0)
    prompt_tokens = models.BigIntegerField(default=0)
    completion_tokens = models.BigIntegerField(default=0)
    # 已知成本的總和；缺價目的呼叫計入 requests/tokens、不計入這裡（詮釋可後補，
    # 事實不可丟）。位數比 usage_logs 寬兩位——這是總和。
    cost = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "platform_usagedaily"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "day", "user_id", "category", "model"],
                name="uq_usagedaily_bucket",
                nulls_distinct=False,
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "day"], name="ix_usagedaily_tenant_day"),
        ]

    def __str__(self) -> str:
        return f"UsageDaily({self.day} {self.model})"


class AuditLog(models.Model):
    """一次敏感操作的紀錄（05 §3.3、04 §8.3；2A-4）。**按月分區、append-only**。

    分區的理由同 `UsageLog`，保留政策不同：稽核依法規 3–7 年（05 §7），冷分區
    DETACH 轉歸檔儲存。

    **append-only 由資料庫擋**（migration 內的 trigger），不是靠「Repository 沒有
    update 方法」的慣例：稽核的價值全部來自「事後不能改」，而一份可以被 UPDATE 的
    稽核表與一份不能被 UPDATE 的稽核表，平常看起來一模一樣。到期清理不受影響
    ——那是 DROP／DETACH 整個分區（DDL），不是 DELETE。

    `created_at` 因此**不用 `auto_now_add`**：append-only 的表事後調不了時間，
    值只能在 INSERT 當下給（回填舊資料與測試都需要）。預設仍是 now。

    `actor_id`／`resource_id` 是裸 UUID 不是 FK，理由同 `UsageLog`：被刪掉的
    使用者與被刪掉的資源，**正是稽核最需要活得比它們久的那兩種**。FK 會讓
    「刪掉使用者」被他自己的稽核紀錄擋住。

    05 §3.3 的欄位表外多三欄（`outcome`／`status`／`permission`），來源是
    「成功與失敗都記、403 帶被拒的 permission code」（10 §3）：塞進 `before`／
    `after` 會讓「資源狀態前後」這個語意說謊，而稽核查詢正是靠它。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="audit_logs")
    # None＝系統行為（維運 job）。塞一個假 uuid 比留白更糟：它看起來像真的有人做了。
    actor_id = models.UUIDField(null=True, blank=True)
    # user / api_key / system。
    actor_type = models.TextField()
    # `resource.verb`（05 §3.3）。註冊表在 api/middleware/audit.py。
    action = models.TextField()
    resource_type = models.TextField()
    resource_id = models.UUIDField(null=True, blank=True)
    # 白名單欄位的前後值（None＝這個操作沒有「前」或「後」，例如建立與刪除）。
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    # succeeded / denied / failed。
    outcome = models.TextField()
    # HTTP 狀態碼；None＝不是請求層記的（登入走 service）。
    status = models.IntegerField(null=True, blank=True)
    # 被拒的 permission code（10 §3）；只有 outcome=denied 有值。
    permission = models.TextField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(default="")
    # 與存取日誌、SSE 事件同一個 id（12 §1.1：一個 ID 查穿全鏈路）。
    request_id = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "platform_auditlog"
        # 索引同 UsageLog：分區表的索引建在父表的 DDL 上（見 migration）。

    def __str__(self) -> str:
        return f"AuditLog({self.action} {self.outcome})"


class Notification(models.Model):
    """一則寄給某個人的通知（05 §3.3、04 §8.5；2A-5）。

    **不分區**：05 §5.2 點名的三張高成長表不含它。通知的量級是「事件數」
    （文件失敗、額度告警），與 `usage_logs` 的「請求數」差三個數量級。

    `user_id` 是裸 UUID 不是 FK，理由同 `UsageLog`：使用者可被匿名化刪除，而
    「這個人被通知過什麼」不該反過來擋住刪除。

    05 §3.3 的欄位表外多兩欄（**待同步文件**）：

    * ``dedupe_key``：同一件事只發一次。quota 的 80% 一個週期一則、一批 ready
      合成一則——去重擋在 DB 的唯一約束上，不是靠 service 先查再插（兩個 worker
      同時完成時各查各的，都會查到「還沒有」）。
    * ``updated_at``：收合要有「最後一次有動靜」的時間可以排序。改 `created_at`
      等於竄改建立時間，收件匣會出現一列「三小時前建立、剛剛才變動」的東西。

    ``channels`` 是真的陣列（05 §3.3 的 `t[]`）而不是 JSON：「哪些通知寄了 email」
    是寄信出問題時第一個要查的東西，而 jsonb 的陣列查得到卻索引不了。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="notifications")
    # 收件人。通知是寄給**某個人**的，不是租戶的公佈欄。
    user_id = models.UUIDField()
    # document.failed / document.ready / quota.threshold / evaluation.completed
    # （訂閱規則在 services/platform/notifications.py 的 CHANNELS_BY_TYPE）
    type = models.TextField()
    title = models.TextField()
    body = models.TextField(default="", blank=True)
    # 實際派送的通道（["in_app"] / ["in_app", "email"]）。
    channels = ArrayField(models.TextField(), default=list)
    # 前端跳轉與去重判定要用的結構化欄位（document_id、resource、threshold…）。
    meta = models.JSONField(default=dict, blank=True)
    # None＝這一則不去重（每一次文件失敗都是一件要處理的事）。
    dedupe_key = models.TextField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "platform_notification"
        constraints = [
            # 條件排除 NULL：不去重的那些必須能重複，否則第二次失敗會被 DB 擋掉，
            # 而使用者永遠不會知道。
            models.UniqueConstraint(
                fields=["tenant", "user_id", "dedupe_key"],
                condition=models.Q(dedupe_key__isnull=False),
                name="uq_notification_dedupe",
            )
        ]
        indexes = [
            # 收件匣與未讀數的唯一查法：這個人的、最近有動靜的在前面。
            models.Index(fields=["tenant", "user_id", "-updated_at"], name="ix_notification_inbox"),
        ]

    def __str__(self) -> str:
        return f"Notification({self.type})"

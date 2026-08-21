"""Platform context 的資料模型（05 §3.3／§3.5：UsageLog、AuditLog、Notification…）。

**Model 保持薄**（鐵則 6）：只有欄位、Meta、`__str__`。

`UsageLog` 是 2A-1 進來的第一張表；AuditLog（2A-4）、Notification（2A-5）依序跟上。
Phase 0 起這個 app 就存在，當時只承載 extension 的 migration（0001_extensions.py）。
"""

from __future__ import annotations

import uuid

from django.db import models

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

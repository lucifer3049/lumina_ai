"""AI context 的資料模型（05 §3.3）。

**Model 保持薄**（鐵則 6）：只有欄位、Meta、`__str__`。

1D-3b 只做 Prompt 這一組；`ModelConfig` 與 `EvaluationRun`（同屬 05 §3.3）分別排
2A 與 3B。

**這裡有一個前面幾個 context 都沒有的形狀：`tenant_id` 可以是 NULL，代表系統模板**
（05 §3.3 原文）。它與 `identity_role` 的系統角色同一種例外，RLS 的處理見
`migrations/0002_rls.py`——那一份的 policy 比其他 context 多一層，而多出來的那層正是
「租戶讀得到共用模板，但寫不出共用模板」。
"""

from __future__ import annotations

import uuid

from django.db import models

from apps.identity.models import Tenant


class TimestampedModel(models.Model):
    """標配三欄（05 §3 前言）：建立、更新、軟刪除時間。

    **這是第四份**（identity／knowledge／conversation 各一份，內容相同）。1D-1 的註解
    寫「三份就該處理了，建議排一個小工作包把它抽到共用模組」，而那個工作包還沒排，
    於是它變成四份——本包沒有動它，因為抽取要動 identity，而那是範圍外的改動
    （CLAUDE.md：範圍外的順手改善一律禁止）。

    在抽出去之前，四份的內容必須完全一致：`deleted_at` 的語意若依表而異，
    `SoftDeletableRepository` 的排除條件就會在某些表上失效，而那不會有任何症狀。
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class Prompt(models.Model):
    """一個 prompt 的**身分**（key、名稱、目前生效的版本）。內容在 `PromptVersion`。

    軟刪除實體（05 §5.4）。`active_version_id` 刻意是裸的 UUID 而不是 FK：Prompt 與
    PromptVersion 互相指涉，做成雙向 FK 會產生循環相依，而 Django 的建表順序處理
    循環 FK 需要 `db_constraint=False` 或延後約束——兩者都是為了一個「指向自己子表
    的指標」而付的代價。切換 active 版本是 Prompt 模組的單一寫入點（04 §5.3），
    有效性由那一層保證。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # **NULL = 系統模板**（05 §3.3）：一份模板服務所有租戶。每個租戶各種一份的話，
    # 改一次系統模板要回填全部租戶，而漏掉的那些會停在舊版本、答得與別人不同。
    tenant = models.ForeignKey(
        Tenant, on_delete=models.PROTECT, related_name="prompts", null=True, blank=True
    )
    key = models.TextField()
    name = models.TextField(default="", blank=True)
    description = models.TextField(default="", blank=True)
    active_version_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "key"], name="uq_prompt_tenant_key"),
            # **NULL 不等於 NULL**：上面那條約束對系統模板完全沒有作用，PostgreSQL
            # 認為每一個 NULL 都是相異的，於是同一個 key 可以有無限多份系統模板，
            # 而查詢會隨機拿到其中一份。partial unique index 才管得住那一半。
            models.UniqueConstraint(
                fields=["key"],
                condition=models.Q(tenant__isnull=True),
                name="uq_prompt_system_key",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "key"], name="ix_prompt_tenant_key"),
        ]

    def __str__(self) -> str:
        return f"{self.key}({'system' if self.tenant_id is None else self.tenant_id})"


class PromptVersion(models.Model):
    """一個版本的**內容**。published 之後不可變（05 §3.3，由 DB trigger 保證）。

    **沒有 `tenant_id` 欄位**：它經 `prompt_id` 隸屬於 Prompt，隔離也走那條路
    （policy 是 `EXISTS (...)` 形式，見 migrations/0002_rls.py）。反正規化一份
    tenant_id 進來會多一個「與父列不一致」的失效模式，而那種列在兩邊的查詢下會
    時而可見時而不可見。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    prompt = models.ForeignKey(Prompt, on_delete=models.CASCADE, related_name="versions")
    version = models.IntegerField()
    # draft / published（13 對 1D 的範圍：版本機制簡化版）。`archived` 屬 Phase 5 的
    # 發佈流程，欄位收得下，現在沒有人寫入它。
    status = models.TextField(default="draft")
    template = models.TextField()
    variables_schema = models.JSONField(default=dict, blank=True)
    model_hint = models.JSONField(default=dict, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.UUIDField(null=True, blank=True)
    change_note = models.TextField(default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # 版本號是 `messages.prompt_version`（05 §3.4）指過來的鍵：同一個 prompt
            # 有兩個 v1 時，那個快照就指不回唯一的一份內容。
            models.UniqueConstraint(fields=["prompt", "version"], name="uq_promptversion_number"),
        ]
        indexes = [
            models.Index(fields=["prompt", "version"], name="ix_promptversion_prompt"),
        ]

    def __str__(self) -> str:
        return f"{self.prompt_id} v{self.version}({self.status})"

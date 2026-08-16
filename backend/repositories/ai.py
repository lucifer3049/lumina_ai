"""Prompt 的 Repository（05 §3.3、1D-3b）。

**這是 repo 內第一個「租戶 + 系統共用」的資料存取**，而它與前面所有 Repository 的
差別只有一行，卻是整個工作包最容易寫錯的一行：`TenantScopedRepository` 的起點是
`filter(tenant_id=current)`，而系統模板的 `tenant_id` 是 NULL——照用的話，**每一次
問答都會找不到模板**，而錯誤會出現在 1D-5，看起來像「問答壞了」。

因此本檔覆寫 `get_queryset()` 放行 NULL，並在同一個地方定死優先序：**租戶自己的
模板勝過系統模板**（05 §3.3 的 `UNIQUE(tenant_id, key)` 就是為這件事留的）。反過來
的話，租戶客製完全不生效，而畫面上那份模板看起來好好的。

**放寬的只有讀**：寫入路徑（Phase 5 的 `/prompts`）不得產生 `tenant_id IS NULL` 的
列，那由 RLS 的 `WITH CHECK` 擋（`apps/ai/migrations/0002_rls.py`）——一個租戶寫得出
系統模板，就等於改得動所有人的 prompt。
"""

from __future__ import annotations

import uuid

from django.db.models import Case, Q, QuerySet, When

from apps.ai.models import Prompt, PromptVersion
from core.tenant import get_current_tenant_id
from repositories.base import SoftDeletableRepository

# 只有這個狀態的版本可以服務線上流量。草稿是「改到一半」——被選中的話，一個還沒
# review 的指令會直接影響所有回答，而它看起來只是「今天的答案怪怪的」。
PUBLISHED = "published"


class PromptRepository(SoftDeletableRepository[Prompt]):
    model = Prompt

    def get_queryset(self) -> QuerySet[Prompt]:
        """本租戶的 + 系統的（`tenant_id IS NULL`）。

        `get_current_tenant_id` 照樣呼叫、照樣在缺 context 時 raise：放行系統模板不等
        於放行「不知道自己是誰」的查詢。少了它，一條忘了設租戶的路徑會安靜地讀到系統
        模板然後繼續跑下去，而 Fail Fast（鐵則 4）要的正是在那一刻停住。
        """
        tenant_id = get_current_tenant_id(operation=f"{type(self).__name__}.get_queryset")
        return self.model._default_manager.filter(
            Q(tenant_id=tenant_id) | Q(tenant_id__isnull=True),
            deleted_at__isnull=True,
        )

    def by_key(self, key: str) -> Prompt | None:
        """依 key 取模板；**租戶自己的優先**。兩者都不存在時回 None。"""
        return (
            self.get_queryset()
            .filter(key=key)
            # `is_system` 是布林：False(0) 排在 True(1) 前面，也就是租戶自己的先。
            # 不用 `order_by("-tenant_id")`——那是拿 UUID 排序，兩個租戶的優先序會
            # 隨機（而單租戶的測試永遠看不出來）。
            .annotate(is_system=Case(When(tenant_id__isnull=True, then=1), default=0))
            .order_by("is_system")
            .first()
        )

    def active_version(self, key: str) -> PromptVersion | None:
        """目前生效中的版本。**「最新」不等於「生效中」**：v2 還在草稿時服務的仍是 v1。

        以 `active_version_id` 定位而不是「最大的版本號」：切換 active 是 rollback 的
        實作方式（04 §5.3），而 rollback 之後最大的版本號正是那個要被退掉的。
        """
        prompt = self.by_key(key)
        if prompt is None or prompt.active_version_id is None:
            return None
        return (
            PromptVersion._default_manager.filter(
                id=prompt.active_version_id, prompt_id=prompt.id, status=PUBLISHED
            )
            .select_related("prompt")
            .first()
        )

    def version_for(self, key: str, *, version: int) -> PromptVersion | None:
        """指定版本——3B 的評測（同一題比較 v1 與 v2）與事故回溯的入口。

        同樣只給 published：釘一個草稿等於把「改到一半」拿去跑評測，而評測的結論會
        被拿來決定要不要上線。
        """
        prompt = self.by_key(key)
        if prompt is None:
            return None
        return (
            PromptVersion._default_manager.filter(
                prompt_id=prompt.id, version=version, status=PUBLISHED
            )
            .select_related("prompt")
            .first()
        )

    def version_by_id(self, version_id: uuid.UUID) -> PromptVersion | None:
        """依 id 取版本（測試與維運用）。隔離仍由 RLS 保證——`ai_promptversion` 的
        policy 走 `EXISTS` 到父表，見 migrations/0002_rls.py。"""
        return PromptVersion._default_manager.filter(id=version_id).first()

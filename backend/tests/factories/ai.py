"""AI context 的 factory（apps/ai/models.py）。

形狀與 `tests/factories/conversation.py` 一致：factory 類別關在模組內，對外只給有回傳
型別的 `make_*` 薄包裝。

**建立資料一律要在租戶 context ＋ 交易內**（`tenant_scope`）：兩張表都有 RLS，而
`ai_prompt` 的 `WITH CHECK` 只放行**自己**租戶的列——系統模板（`tenant_id IS NULL`）
只有 migration 建得出來（那條 policy 限定 owner，見 migrations/0002_rls.py），
factory 刻意不提供建立它的捷徑：測試若能繞過那條規則，那條規則就等於沒有被測到。
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import factory

from apps.ai.models import Prompt, PromptVersion


class PromptFactory(factory.django.DjangoModelFactory[Prompt]):
    class Meta:
        model = Prompt

    id = factory.LazyFunction(uuid.uuid4)
    tenant = factory.SubFactory("tests.factories.identity.TenantFactory")
    key = factory.Sequence(lambda n: f"prompt-{n}")
    name = factory.Sequence(lambda n: f"模板 {n}")
    description = ""
    active_version_id = None


class PromptVersionFactory(factory.django.DjangoModelFactory[PromptVersion]):
    class Meta:
        model = PromptVersion

    id = factory.LazyFunction(uuid.uuid4)
    prompt = factory.SubFactory(PromptFactory)
    version = 1
    status = "draft"
    template = "測試模板"
    variables_schema: dict[str, Any] = {}
    model_hint: dict[str, Any] = {}
    change_note = ""


def _resolve(kwargs: dict[str, Any]) -> dict[str, Any]:
    """`tenant_id=` 的糖：與其他 factory 一致，呼叫端傳 UUID 而不是 model 實例。"""
    if "tenant_id" in kwargs:
        from apps.identity.models import Tenant

        kwargs["tenant"] = Tenant.objects.get(id=kwargs.pop("tenant_id"))
    return kwargs


def make_prompt(**kwargs: Any) -> Prompt:
    return cast(Prompt, PromptFactory(**_resolve(kwargs)))


def make_prompt_version(**kwargs: Any) -> PromptVersion:
    return cast(PromptVersion, PromptVersionFactory(**kwargs))

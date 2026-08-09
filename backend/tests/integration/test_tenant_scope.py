"""A 組正確性測試 —— 租戶隔離的第一道防線（ADR-002：Repository 的 tenant filter）。

第二道（DB 的 RLS policy）由 ``test_rls_identity.py`` 驗；兩道分開測是刻意的，
因為它們會以不同方式失效：這裡驗的是「程式沒有繞過 filter」，那裡驗的是「就算
程式繞過了，DB 也擋得住」。

DB 存取經 ``two_tenants`` fixture 取得（它依賴 ``transactional_db``）——
理由見 tests/conftest.py。
"""

from __future__ import annotations

import uuid

from core.db import run_orm
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import UserRepository


def _emails_of_current_tenant() -> set[str]:
    """在 threadpool 執行緒上讀當前租戶的使用者。

    包 ``unit_of_work``：RLS policy 讀的 ``app.tenant_id`` 是**交易區域**參數，
    沒有交易就沒有那個值，查詢會回空集合——而空集合正好與「隔離有效」長得一樣，
    測試會假綠燈（05 §5.1）。
    """
    with unit_of_work():
        return set(UserRepository().get_queryset().values_list("email", flat=True))


class TestTenantIsolation:
    async def test_each_tenant_sees_only_own_rows(
        self, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_a, tenant_b = two_tenants

        with tenant_context(tenant_a):
            emails_a = await run_orm(_emails_of_current_tenant)
        with tenant_context(tenant_b):
            emails_b = await run_orm(_emails_of_current_tenant)

        assert emails_a == {"a-0@example.com", "a-1@example.com", "a-2@example.com"}
        assert emails_b == {"b-0@example.com", "b-1@example.com", "b-2@example.com"}

    async def test_count_is_tenant_scoped(self, two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        """筆數也必須是租戶內的——聚合查詢是最容易漏掉 filter 的一種。"""
        tenant_a, _ = two_tenants

        def _count() -> int:
            with unit_of_work():
                return UserRepository().get_queryset().count()

        with tenant_context(tenant_a):
            assert await run_orm(_count) == 3

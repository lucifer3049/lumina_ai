"""A 組正確性測試 —— 租戶隔離（ADR-002）。

DB 存取經 ``two_tenants`` fixture 取得（它依賴 ``transactional_db``）——
理由見 tests/conftest.py。
"""

from __future__ import annotations

import uuid

from core.db import run_orm
from core.tenant import tenant_context
from repositories.spike import SpikeItemRepository


class TestTenantIsolation:
    async def test_each_tenant_sees_only_own_rows(
        self, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_a, tenant_b = two_tenants
        repo = SpikeItemRepository()

        with tenant_context(tenant_a):
            rows_a = await run_orm(repo.latest, 100)
        with tenant_context(tenant_b):
            rows_b = await run_orm(repo.latest, 100)

        assert {r["title"] for r in rows_a} == {"a-0", "a-1", "a-2"}
        assert {r["title"] for r in rows_b} == {"b-0", "b-1", "b-2"}

    async def test_count_is_tenant_scoped(self, two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        tenant_a, _ = two_tenants
        repo = SpikeItemRepository()
        with tenant_context(tenant_a):
            assert await run_orm(repo.count_all) == 3

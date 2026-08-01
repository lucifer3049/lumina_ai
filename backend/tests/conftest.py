"""測試共用 fixture。

**雙租戶是預設，不是選項**（CLAUDE.md 測試規範）：只有一個租戶的測試無法
證明隔離有效——查詢當然只會回傳那個租戶的資料。所以 fixture 一律建兩個租戶
並各自塞資料，隔離斷言才有意義。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from apps.spike.models import SpikeItem
from core.tenant import tenant_context

TENANT_A = uuid.UUID("11111111-1111-5111-8111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-5222-8222-222222222222")


@pytest.fixture
def two_tenants(transactional_db: object) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """建立兩個租戶各 3 筆資料。

    用 ``transactional_db`` 而非 ``db``：run_orm 把查詢送到另一條執行緒，
    pytest-django 預設的交易包裹在那頭看不到，測試會以假失敗誤導人。
    """
    SpikeItem.objects.bulk_create(
        [SpikeItem(tenant_id=TENANT_A, title=f"a-{i}") for i in range(3)]
        + [SpikeItem(tenant_id=TENANT_B, title=f"b-{i}") for i in range(3)]
    )
    yield TENANT_A, TENANT_B


@pytest.fixture
def as_tenant_a() -> Iterator[uuid.UUID]:
    with tenant_context(TENANT_A) as tid:
        yield tid

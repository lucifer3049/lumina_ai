"""驗收：一則訊息只查一次限額（二次架構審計 F-03）。

`QuotaService.limits()` 每次呼叫都開一組 `tenant_context + unit_of_work`。而
`ChatService.start_turn` 連呼三次 `check_and_reserve`（messages_day / tokens_month /
streams），上傳路徑是兩次——同一個租戶、同一份限額表，在同一個請求裡查三到四遍。

**這條測試量的是交易數，不是時間。** 量時間會得到一個隨機器而變的數字，而且在
小資料量下差異被雜訊蓋過；交易數是這個最佳化的直接定義，改壞了立刻看得出來。

反向的錯法同樣要擋：**沒有請求邊界時不可以快取**。Celery task 與管理指令的「一次」
可能跨越好幾分鐘，在那裡記住限額等於做出一個改了方案也不生效的全域變數。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from core.request_cache import request_cache
from services.platform.quota import QuotaService
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def tenant() -> Iterator[None]:
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a", settings={"quota": {"messages_day": 7}})
    yield


class _CountingTenants:
    """包住真的 repository，數 `current()` 被呼叫幾次——那是限額查詢的唯一 DB 動作。"""

    def __init__(self) -> None:
        from repositories.identity import TenantRepository

        self._inner = TenantRepository()
        self.calls = 0

    def current(self) -> object:
        self.calls += 1
        return self._inner.current()


class TestInsideARequest:
    def test_three_lookups_hit_the_database_once(self, tenant: None) -> None:
        """start_turn 的形狀：同一個租戶連問三次限額。"""
        tenants = _CountingTenants()
        service = QuotaService(tenants=tenants)  # type: ignore[arg-type]

        with request_cache():
            for _ in range(3):
                assert service.limits(TENANT_A)["messages_day"] == 7

        assert tenants.calls == 1, f"查了 {tenants.calls} 次，應該只有一次"

    def test_the_caller_cannot_poison_the_memo(self, tenant: None) -> None:
        """回傳的是副本。交出同一個 dict 的話，任何呼叫端的就地修改都會污染
        本請求後續的每一次查詢——而那不會有錯誤訊息。"""
        service = QuotaService()

        with request_cache():
            first = service.limits(TENANT_A)
            first["messages_day"] = 999_999

            assert service.limits(TENANT_A)["messages_day"] == 7


class TestOutsideARequest:
    def test_a_worker_without_a_request_boundary_always_reloads(self, tenant: None) -> None:
        """Celery task 走這條路。記住限額等於「改了方案也不生效」，而那個 worker
        行程可能跑好幾天。"""
        tenants = _CountingTenants()
        service = QuotaService(tenants=tenants)  # type: ignore[arg-type]

        for _ in range(3):
            service.limits(TENANT_A)

        assert tenants.calls == 3

"""驗收：收尾時 quota key 一定帶著到期時間（二次架構審計 F-05）。

`INCRBY` / `DECRBY` 會**建立**不存在的 key，而建出來的那個沒有 TTL。所以
「key 已經不在了，而 `commit()` / `release()` 又把它變回來」的順序，會復活一個
**永不過期**的計數器——它一路活到下一次日結對帳（`correct()` 帶 `ex=`）才被蓋掉，
期間那個租戶的額度看起來一直是用掉的。

第一輪審計把這條評為 Medium，第二輪把它降成 Low：宣稱的兩條觸發路徑（期別翻頁、
超過一小時的生成）其實都被擋著——期別 key 的 TTL 是「期末 + 1 天」的 grace，而聊天
有 120 秒的總逾時。剩下的是 **Redis 資料遺失類**：AOF 沒回放、有人手動 DEL、
failover 到一個落後的副本。成本兩行，所以還是做。

**`NX` 是這一段的重點**，不是細節：無條件 `EXPIRE` 會把每一次收尾都變成續命，
月額度的 key 於是永遠不會到期——那比原問題更糟（原問題要 Redis 先掉資料才會發生，
續命則是每天都在發生）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest

from core.redis import get_redis
from services.platform.quota import QuotaReservation, QuotaService
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)

_SMALL = {"tokens_month": 1000, "messages_day": 10, "streams": 2}


@pytest.fixture
def tenant() -> Iterator[None]:
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a", settings={"quota": dict(_SMALL)})
    yield
    client = get_redis()
    keys = list(client.scan_iter(match=f"t:{TENANT_A}:*"))
    if keys:
        client.delete(*keys)


def _ttl(key: str) -> int:
    """key 的剩餘秒數。`-1` = 存在但永不過期（**正是本檔要擋的東西**），`-2` = 不存在。"""
    return int(cast("int", get_redis().ttl(key)))


def _reserve(resource: str, amount: int) -> QuotaReservation:
    reservation = QuotaService().check_and_reserve(TENANT_A, resource, amount)
    assert reservation is not None, f"{resource} 應該有限額才驗得到這件事"
    return reservation


class TestResurrectedKeysGetAnExpiry:
    def test_release_after_the_key_vanished_leaves_a_ttl(self, tenant: None) -> None:
        """模擬 Redis 掉資料：key 不見了，然後生成失敗走 `release()`。

        沒有這道防禦時，`DECRBY` 會建出一個 TTL 為 -1 的 key。
        """
        reservation = _reserve("tokens_month", 100)
        get_redis().delete(reservation.key)
        assert _ttl(reservation.key) == -2, "前提：key 真的不在了"

        QuotaService().release(reservation)

        assert _ttl(reservation.key) > 0, "key 被 DECRBY 復活了，而且永不過期"

    def test_commit_after_the_key_vanished_leaves_a_ttl(self, tenant: None) -> None:
        """另一條路：生成成功、以實際值校正，而 key 在那之前不見了。"""
        reservation = _reserve("tokens_month", 100)
        get_redis().delete(reservation.key)

        QuotaService().commit(reservation, actual=250)

        assert _ttl(reservation.key) > 0

    def test_the_gauge_resource_is_covered_too(self, tenant: None) -> None:
        """`streams` 是瞬時值，key 的形狀與 TTL 都與期別資源不同——兩條路都要走到。"""
        reservation = _reserve("streams", 1)
        get_redis().delete(reservation.key)

        QuotaService().release(reservation)

        assert _ttl(reservation.key) > 0


class TestItDoesNotExtendALivingKey:
    def test_an_existing_expiry_is_left_alone(self, tenant: None) -> None:
        """**`NX` 的理由。** 無條件 `EXPIRE` 會把每次收尾變成續命，月額度的 key
        於是永遠不會到期——比原問題更糟，因為它每天都在發生。

        做法：把 TTL 壓成一個很小的值，收尾後它應該**還是**那個小值（沒有被推回
        原本的期別長度）。
        """
        reservation = _reserve("tokens_month", 100)
        get_redis().expire(reservation.key, 30)

        QuotaService().release(reservation)

        assert 0 < _ttl(reservation.key) <= 30, "既有的到期時間被續命了"

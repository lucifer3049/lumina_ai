"""驗收：Quota 計數器——Redis 上的 reserve/commit/release（04 §8.1，2A-2a）。

即時計數住 Redis（04 §8.1；DB 快照與對帳屬 2A-2b）。**週期重置不用排程**：
key 按期別命名（月 `YYYY-MM`、日 `YYYY-MM-DD`）＋ TTL——新的一期自然從 0 開始，
沒有「重置 job 沒跑」這種故障模式。

reserve/commit 兩段式的理由：token 的實際用量要到生成**結束**才知道，而擋線必須
畫在**開始**之前。於是開場先按估計值預留（併發下不會集體衝過線），結束時按
usage 事件的實際值校正。

三件事錯了都不會有例外：

1. **key 沒帶租戶前綴**（鐵則 4）。所有租戶共用一個計數器，症狀是「大家一起被
   一個大戶擋住」，而每個租戶自己的畫面都說「你用超了」。
2. **release 減過頭**。重複 release（重試、錯誤路徑各自清理）把計數器減成負的，
   等於送額度。
3. **週期 key 沒有 TTL**。計數器永不過期，Redis 慢慢塞滿死 key——它不會壞，
   只會愈來愈貴。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from services.platform.quota import QuotaExceededError, QuotaService

from core.redis import get_redis, tenant_key
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)

# 免費方案的預設太大，測試會需要幾十萬次 reserve 才碰得到線——直接把租戶的
# 覆寫調小（走與正式相同的解析路徑，不是測試後門）。
_SMALL = {"tokens_month": 100, "messages_day": 3, "streams": 2}


@pytest.fixture
def tenants() -> Iterator[None]:
    for tenant_id, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=slug, settings={"quota": dict(_SMALL)})
    yield
    client = get_redis()
    for tenant_id in (TENANT_A, TENANT_B):
        keys = list(client.scan_iter(match=tenant_key(tenant_id, "*")))
        if keys:
            client.delete(*keys)


def _used(tenant_id: uuid.UUID, resource: str) -> int:
    status = {s.resource: s for s in QuotaService().status(tenant_id)}
    return status[resource].used


class TestReserve:
    def test_reserve_accumulates(self, tenants: None) -> None:
        service = QuotaService()
        service.check_and_reserve(TENANT_A, "tokens_month", 40)
        service.check_and_reserve(TENANT_A, "tokens_month", 40)

        assert _used(TENANT_A, "tokens_month") == 80

    def test_crossing_the_limit_raises_with_details(self, tenants: None) -> None:
        """429 的 body 要能告訴使用者「哪個資源、上限多少」——details 是 client
        唯一拿得到的機器可讀資訊（09 §1.3）。"""
        service = QuotaService()
        service.check_and_reserve(TENANT_A, "tokens_month", 80)

        with pytest.raises(QuotaExceededError) as exc_info:
            service.check_and_reserve(TENANT_A, "tokens_month", 40)

        details = exc_info.value.details
        assert details["resource"] == "tokens_month"
        assert details["limit"] == 100
        assert exc_info.value.code.value == "QUOTA_EXCEEDED"

    def test_a_failed_reserve_does_not_consume(self, tenants: None) -> None:
        """被擋的那一次不能留下痕跡——否則被擋幾次之後，額度被「失敗」吃光。"""
        service = QuotaService()
        service.check_and_reserve(TENANT_A, "tokens_month", 80)
        with pytest.raises(QuotaExceededError):
            service.check_and_reserve(TENANT_A, "tokens_month", 40)

        assert _used(TENANT_A, "tokens_month") == 80

    def test_an_unlimited_resource_is_a_no_op(self, tenants: None) -> None:
        """None＝不限制：不碰 Redis、回 None——沒有 key 就沒有洩漏的 key。"""
        unlimited_tenant = uuid.uuid4()
        with tenant_scope(unlimited_tenant):
            make_tenant(
                id=unlimited_tenant,
                slug="tenant-unlimited",
                settings={"quota": {"tokens_month": None}},
            )

        service = QuotaService()
        reservation = service.check_and_reserve(unlimited_tenant, "tokens_month", 10**9)

        assert reservation is None
        client = get_redis()
        assert list(client.scan_iter(match=tenant_key(unlimited_tenant, "quota", "*"))) == []

    def test_tenants_do_not_share_counters(self, tenants: None) -> None:
        service = QuotaService()
        service.check_and_reserve(TENANT_A, "tokens_month", 100)

        service.check_and_reserve(TENANT_B, "tokens_month", 100)  # 不該被 A 擋

        assert _used(TENANT_A, "tokens_month") == 100
        assert _used(TENANT_B, "tokens_month") == 100


class TestCommitAndRelease:
    def test_commit_adjusts_to_the_actual_amount(self, tenants: None) -> None:
        """預留 80、實際 15 → 計數器是 15。不校正的話，額度按「最悲觀的估計」
        消耗，月中就會有人被擋，而帳面明明沒用完。"""
        service = QuotaService()
        reservation = service.check_and_reserve(TENANT_A, "tokens_month", 80)
        assert reservation is not None

        service.commit(reservation, actual=15)

        assert _used(TENANT_A, "tokens_month") == 15

    def test_release_returns_the_reservation(self, tenants: None) -> None:
        service = QuotaService()
        reservation = service.check_and_reserve(TENANT_A, "streams", 1)
        assert reservation is not None

        service.release(reservation)

        assert _used(TENANT_A, "streams") == 0

    def test_release_never_goes_below_zero(self, tenants: None) -> None:
        """錯誤路徑常常各自清理，重複 release 必然發生——它不能變成送額度。"""
        service = QuotaService()
        reservation = service.check_and_reserve(TENANT_A, "streams", 1)
        assert reservation is not None

        service.release(reservation)
        service.release(reservation)

        assert _used(TENANT_A, "streams") == 0


class TestKeyHygiene:
    def test_every_quota_key_is_tenant_prefixed_and_expiring(self, tenants: None) -> None:
        """鐵則 4 的 `t:{tenant_id}:` 前綴＋每把 key 都有 TTL（週期 key 過期＝
        重置；gauge 帶安全 TTL，行程死掉不會永久佔位）。"""
        service = QuotaService()
        service.check_and_reserve(TENANT_A, "tokens_month", 1)
        service.check_and_reserve(TENANT_A, "messages_day", 1)
        service.check_and_reserve(TENANT_A, "streams", 1)

        client = get_redis()
        keys = list(client.scan_iter(match=tenant_key(TENANT_A, "quota", "*")))
        assert len(keys) >= 3, "quota 計數器必須住在 t:{tenant}:quota: 之下"
        for key in keys:
            assert client.ttl(key) > 0, f"{key!r} 沒有 TTL"

    def test_day_and_month_periods_use_separate_keys(self, tenants: None) -> None:
        """日與月的計數各自歸零——共用一把 key 的話，其中一種的語意一定是錯的。"""
        service = QuotaService()
        service.check_and_reserve(TENANT_A, "tokens_month", 1)
        service.check_and_reserve(TENANT_A, "messages_day", 1)

        client = get_redis()
        keys = {
            key.decode() if isinstance(key, bytes) else key
            for key in client.scan_iter(match=tenant_key(TENANT_A, "quota", "*"))
        }
        month_keys = [k for k in keys if "tokens_month" in k]
        day_keys = [k for k in keys if "messages_day" in k]
        assert month_keys and day_keys
        assert set(month_keys).isdisjoint(day_keys)

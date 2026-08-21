"""驗收：per-tenant 公平佇列（08 §6 背壓，1B 帶進 2A 的缺口④，2A-2b）。

單一租戶把 500 份文件一次倒進來時，etl 佇列是 FIFO——其他租戶的一份小文件要排在
500 份後面，SLO（分鐘級 ready）對他們而言直接失效，而系統每個指標都是綠的
（鄰居效應）。

做法：**worker 端的租戶並發閘**。task 開跑前先取該租戶的 slot（Redis 計數，
上限 `etl_max_concurrent_per_tenant`）；拿不到就把自己重新排隊（帶延遲）並立刻
讓出 worker——佇列裡的下一個（別的租戶）馬上有人服務。選 worker 端而不是入隊端
限流：上傳不能因為佇列滿而失敗（1B-3：送不出去不讓上傳失敗），推遲的該是處理、
不是收件。

三件事錯了都不會有例外：

1. **slot 沒還**。任務結束（成功、失敗、文件不見了）都要歸還，漏一條路徑，
   那個租戶的 ETL 慢慢降到零——症狀是「他的文件全部卡住，別人都正常」。
2. **讓位變成丟棄**。拿不到 slot 直接 return 而沒有重新排隊，文件永遠停在
   uploaded，且沒有任何錯誤。
3. **計數器跨租戶共用**（鐵則 4 的 key 前綴）——變成全域上限，公平蕩然無存。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from config.settings.app_settings import get_app_settings
from core.redis import get_redis, tenant_key
from services.platform.fairness import TenantSlotLimiter
from tests.conftest import TENANT_A, TENANT_B

pytestmark = pytest.mark.django_db(transaction=True)


def _capture_into(store: list[dict[str, Any]]) -> Any:
    def _fake_enqueue(**kwargs: Any) -> str:
        store.append(kwargs)
        return "task-id"

    return _fake_enqueue


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    for tenant_id in (TENANT_A, TENANT_B):
        keys = list(client.scan_iter(match=tenant_key(tenant_id, "*")))
        if keys:
            client.delete(*keys)


class TestDefaults:
    def test_the_cap_and_requeue_delay_are_tunable(self) -> None:
        """兩個數字都是營運要調的（15 §4.1）：上限決定公平的粒度，延遲決定
        被讓位的任務多快回來再試。"""
        settings = get_app_settings()

        assert settings.etl_max_concurrent_per_tenant == 2
        assert settings.etl_fairness_requeue_seconds == 15


class TestSlots:
    def test_acquire_up_to_the_cap_then_refuse(self) -> None:
        limiter = TenantSlotLimiter("etl")

        assert limiter.acquire(TENANT_A) is True
        assert limiter.acquire(TENANT_A) is True
        assert limiter.acquire(TENANT_A) is False, "第三個要被拒絕（上限 2）"

    def test_release_frees_a_slot(self) -> None:
        limiter = TenantSlotLimiter("etl")
        limiter.acquire(TENANT_A)
        limiter.acquire(TENANT_A)

        limiter.release(TENANT_A)

        assert limiter.acquire(TENANT_A) is True

    def test_a_refused_acquire_does_not_occupy(self) -> None:
        """被拒絕的那一次不能佔位——否則排隊的任務每回來探一次，計數就多一，
        很快連正主都進不來。"""
        limiter = TenantSlotLimiter("etl")
        limiter.acquire(TENANT_A)
        limiter.acquire(TENANT_A)
        limiter.acquire(TENANT_A)  # 被拒絕
        limiter.release(TENANT_A)

        assert limiter.acquire(TENANT_A) is True

    def test_tenants_have_independent_slots(self) -> None:
        limiter = TenantSlotLimiter("etl")
        limiter.acquire(TENANT_A)
        limiter.acquire(TENANT_A)

        assert limiter.acquire(TENANT_B) is True, "A 滿了不關 B 的事"

    def test_slot_keys_are_tenant_prefixed_with_ttl(self) -> None:
        """`t:{tenant}:` 前綴（鐵則 4）＋安全 TTL（worker 被 OOM 砍掉時 slot
        不能永久佔住——那會讓這個租戶的 ETL 慢慢降到零）。"""
        limiter = TenantSlotLimiter("etl")
        limiter.acquire(TENANT_A)

        client = get_redis()
        keys = list(client.scan_iter(match=tenant_key(TENANT_A, "*")))
        assert keys, "slot 計數必須住在租戶前綴之下"
        for key in keys:
            assert int(client.ttl(key)) > 0, f"{key!r} 沒有 TTL"  # type: ignore[arg-type]


class TestWorkerDeference:
    """task 層的行為：拿不到 slot → 重新排隊（帶延遲）、不處理、不佔位。"""

    def _fill_slots(self, tenant_id: uuid.UUID) -> None:
        limiter = TenantSlotLimiter("etl")
        for _ in range(get_app_settings().etl_max_concurrent_per_tenant):
            assert limiter.acquire(tenant_id)

    def test_ingest_defers_when_the_tenant_is_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import worker.etl_tasks as etl_tasks

        requeued: list[dict[str, Any]] = []
        monkeypatch.setattr(
            etl_tasks,
            "enqueue_ingestion",
            _capture_into(requeued),
        )
        self._fill_slots(TENANT_A)
        document_id = uuid.uuid4()

        result = etl_tasks.ingest_document(str(TENANT_A), str(document_id))

        assert result["status"] == "deferred"
        assert len(requeued) == 1
        assert requeued[0]["document_id"] == document_id
        assert requeued[0]["delay_seconds"] == 15, "重新排隊必須帶延遲，否則變成忙迴圈"

    def test_ingest_releases_the_slot_even_when_the_document_is_gone(self) -> None:
        """文件不見了（NotFoundError 路徑）也要歸還——那是最容易漏的一條。"""
        import worker.etl_tasks as etl_tasks

        etl_tasks.ingest_document(str(TENANT_A), str(uuid.uuid4()))

        limiter = TenantSlotLimiter("etl")
        for _ in range(get_app_settings().etl_max_concurrent_per_tenant):
            assert limiter.acquire(TENANT_A), "跑完之後所有 slot 都要是空的"

    def test_embedding_defers_when_the_tenant_is_full(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """embedding 佇列同一套閘：ingest 一結束就會排 embedding，只擋前半段的話，
        洪水只是換一條佇列淹。"""
        import worker.embedding_tasks as embedding_tasks

        requeued: list[dict[str, Any]] = []
        monkeypatch.setattr(
            embedding_tasks,
            "enqueue_embedding",
            _capture_into(requeued),
        )
        limiter = TenantSlotLimiter("embedding")
        for _ in range(get_app_settings().etl_max_concurrent_per_tenant):
            assert limiter.acquire(TENANT_A)

        result = embedding_tasks.embed_document(str(TENANT_A), str(uuid.uuid4()))

        assert result["status"] == "deferred"
        assert len(requeued) == 1

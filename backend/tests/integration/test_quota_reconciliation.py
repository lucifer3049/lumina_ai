"""驗收：quota 日結對帳（04 §8.1「對帳（DB usage_logs 日結校正）」，2A-2b）。

Redis 計數器是**推測**（reserve/commit 的即時值），DB 是**事實**（usage_logs、
messages、documents）。兩者會漂：Redis flush、行程死在 commit 之前、2A-2a 記下的
存量競態。對帳把推測擺回事實，並在 quota_counters 留下快照。

事實來源（每種資源各自的、不是同一張表）：

- `tokens_month`  → usage_logs 的 llm 列（prompt+completion 當月總和）
- `messages_day`  → 當日建立的 assistant 訊息數（**不是** usage_logs：被中止且
  provider 未回報的回合沒有 usage 列，但那一輪確實發生過）
- `documents`／`storage_bytes` → 文件表聚合（2A-2a 已用它擋線，快照只是留痕）
- `streams` 不對帳——瞬時值沒有「應該是多少」可言

三件事錯了都不會有例外：

1. **對帳把錯的方向寫回去**（拿 Redis 蓋 DB）。推測覆蓋事實，漂移從此永久化。
2. **重跑不冪等**。日結 job 重試或手動補跑，同一期出現多列快照，報表隨之跳動。
3. **漏掉一個租戶**。逐租戶迴圈少了誰，誰的計數器就永遠不會被校正——而它平常
   與正確值一致，只在出過事的租戶身上錯。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from services.platform.reconciliation import QuotaReconciliationService

from apps.platform.models import QuotaCounter, UsageLog
from core.redis import get_redis, tenant_key
from services.platform.quota import QuotaService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.conversation import make_conversation, make_message
from tests.factories.identity import make_tenant, make_user, tenant_scope
from tests.factories.knowledge import make_document, make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def tenants() -> Iterator[dict[uuid.UUID, uuid.UUID]]:
    users: dict[uuid.UUID, uuid.UUID] = {}
    for tenant_id, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=slug)
            users[tenant_id] = make_user(tenant_id=tenant_id, email=f"o@{slug}.example").id
    yield users
    client = get_redis()
    for tenant_id in (TENANT_A, TENANT_B):
        keys = list(client.scan_iter(match=tenant_key(tenant_id, "*")))
        if keys:
            client.delete(*keys)


def _seed_llm_usage(tenant_id: uuid.UUID, *, prompt: int, completion: int) -> None:
    with tenant_scope(tenant_id):
        UsageLog.objects.create(
            tenant_id=tenant_id,
            category="llm",
            model="mock-chat",
            prompt_tokens=prompt,
            completion_tokens=completion,
            request_id=str(uuid.uuid4()),
        )


def _seed_assistant_messages(tenant_id: uuid.UUID, owner_id: uuid.UUID, count: int) -> None:
    with tenant_scope(tenant_id):
        conversation = make_conversation(tenant_id=tenant_id, user_id=owner_id)
        for index in range(count):
            make_message(conversation=conversation, role="assistant", content=f"回 {index}")


def _used(tenant_id: uuid.UUID, resource: str) -> int:
    return {s.resource: s for s in QuotaService().status(tenant_id)}[resource].used


def _snapshot(tenant_id: uuid.UUID, resource: str) -> QuotaCounter:
    with tenant_scope(tenant_id):
        return QuotaCounter.objects.get(tenant_id=tenant_id, resource=resource)


class TestTokensMonth:
    def test_the_redis_counter_is_corrected_to_the_ledger(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        """帳上 300、計數器 5000（reserve 沒 commit 的殘留）→ 對帳後計數器 300。"""
        _seed_llm_usage(TENANT_A, prompt=200, completion=100)
        QuotaService().check_and_reserve(TENANT_A, "tokens_month", 5000)

        QuotaReconciliationService().reconcile_tenant(TENANT_A)

        assert _used(TENANT_A, "tokens_month") == 300

    def test_the_snapshot_records_usage_and_limit(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        _seed_llm_usage(TENANT_A, prompt=200, completion=100)

        QuotaReconciliationService().reconcile_tenant(TENANT_A)

        row = _snapshot(TENANT_A, "tokens_month")
        assert row.used == 300
        assert row.period == "month"
        assert row.period_start == datetime.now(UTC).date().replace(day=1)
        assert row.limit == 1_000_000  # free plan 的當時值——歷史報表要知道當時的上限


class TestMessagesDay:
    def test_the_truth_is_assistant_messages_not_usage_rows(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        """3 則 assistant 訊息、0 列 usage（全是被中止的回合）→ 對帳後計數器 3。"""
        _seed_assistant_messages(TENANT_A, tenants[TENANT_A], 3)

        QuotaReconciliationService().reconcile_tenant(TENANT_A)

        assert _used(TENANT_A, "messages_day") == 3
        assert _snapshot(TENANT_A, "messages_day").used == 3


class TestStockResources:
    def test_documents_and_storage_are_snapshotted(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        with tenant_scope(TENANT_A):
            kb = make_knowledge_base(tenant_id=TENANT_A)
            make_document(kb=kb, size_bytes=111)
            make_document(kb=kb, size_bytes=222)

        QuotaReconciliationService().reconcile_tenant(TENANT_A)

        assert _snapshot(TENANT_A, "documents").used == 2
        assert _snapshot(TENANT_A, "storage_bytes").used == 333


class TestIdempotencyAndCoverage:
    def test_rerunning_updates_in_place(self, tenants: dict[uuid.UUID, uuid.UUID]) -> None:
        """同一期跑兩次 = 一列，used 是最新值（upsert）。"""
        service = QuotaReconciliationService()
        _seed_llm_usage(TENANT_A, prompt=100, completion=0)
        service.reconcile_tenant(TENANT_A)
        _seed_llm_usage(TENANT_A, prompt=100, completion=0)

        service.reconcile_tenant(TENANT_A)

        with tenant_scope(TENANT_A):
            rows = QuotaCounter.objects.filter(tenant_id=TENANT_A, resource="tokens_month")
            assert rows.count() == 1
            assert rows.get().used == 200

    def test_reconcile_all_covers_every_active_tenant(
        self, tenants: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        _seed_llm_usage(TENANT_A, prompt=10, completion=0)
        _seed_llm_usage(TENANT_B, prompt=20, completion=0)

        processed = QuotaReconciliationService().reconcile_all()

        assert processed >= 2
        assert _snapshot(TENANT_A, "tokens_month").used == 10
        assert _snapshot(TENANT_B, "tokens_month").used == 20

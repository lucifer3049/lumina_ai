"""驗收：停滯重建 job 的補償掃描（2B-6 帶出的缺口③）。

`enqueue_reindex` 與 repo 內其他 `enqueue_*` 一樣是 best-effort——broker 送不出去時
記一筆 log 就走，不讓使用者的請求失敗（job 已經在 DB 裡）。代價是那個 job 會停在
`pending` 而**沒有任何訊息存在**：不會有人重試，因為沒有東西可以重試。

**它比文件的同一個缺口嚴重**：`pending` 也是「進行中」，而「同一個 KB 只能有一個
進行中的 job」是 DB 約束——停住的那一個會把這個 KB 的重建**永久卡死**，使用者連再按
一次的路都沒有（回 409）。

兩種停滯的處置**刻意不同**：

- **`pending`（還沒開始）→ 補送**。這一版還沒有任何向量被算出來，重送最多是多查一次。
- **`rechunking` / `embedding`（做到一半）→ 標成 `failed`**，不補送。補送等於讓兩個
  `advance` 併行，而它們會各自列出「還缺向量的 chunk」然後**各算一次**——同一批
  chunk 付兩次錢，那正是這個工作包從頭到尾在避免的事。標成 failed 之後使用者可以重新
  發起，而已經算好的向量留著（`chunks_without_embedding` 會跳過它們），一毛都不浪費。

門檻要夠大：**重建期間有一段是合法地什麼都不做**（重切之後等 ETL 把文件跑回 ready）。
那段期間 job 靠**心跳**（每輪把 `updated_at` 推回去）證明自己還活著——沒有心跳的話，
一個正常的大型重建會在 30 分鐘後被這支掃描器判死。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from apps.knowledge.models import KbReindexJob
from services.knowledge.rescue import StuckReindexRescueService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_kb_reindex_job, make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def tenants() -> None:
    for tenant_id, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=slug)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """攔下補送——這一層驗的是「誰被送出去」，不是 Celery 本身。"""
    calls: list[uuid.UUID] = []

    def _fake(*, tenant_id: uuid.UUID, job_id: uuid.UUID, delay_seconds: int = 0) -> str | None:
        calls.append(job_id)
        return "task-id"

    monkeypatch.setattr("services.knowledge.rescue.enqueue_reindex", _fake)
    return calls


def _job(
    tenant_id: uuid.UUID, *, status: str, stale_minutes: int, kb_id: uuid.UUID | None = None
) -> uuid.UUID:
    """一個指定狀態、指定「多久沒動靜」的 job。

    `updated_at` 是 `auto_now`，只能在建立之後用 `update()` 蓋——那正是這支掃描器
    依賴的欄位，因此測試也必須以同一種方式把它調老。
    """
    with tenant_scope(tenant_id):
        kb = make_knowledge_base(tenant_id=tenant_id) if kb_id is None else None
        job = make_kb_reindex_job(
            tenant_id=tenant_id,
            kb_id=kb_id if kb is None else kb.id,
            status=status,
        )
        KbReindexJob.objects.filter(id=job.id).update(
            updated_at=datetime.now(UTC) - timedelta(minutes=stale_minutes)
        )
        return uuid.UUID(str(job.id))


def _reload(tenant_id: uuid.UUID, job_id: uuid.UUID) -> Any:
    with tenant_scope(tenant_id):
        return KbReindexJob.objects.get(id=job_id)


class TestPendingJobsAreRequeued:
    def test_a_stale_pending_job_is_sent_again(self, tenants: None, sent: list[uuid.UUID]) -> None:
        """訊息掉了的唯一恢復路徑——而它同時把這個 KB 從「永久 409」裡放出來。"""
        job_id = _job(TENANT_A, status="pending", stale_minutes=60)

        rescued = StuckReindexRescueService().rescue_tenant(TENANT_A)

        assert rescued == 1
        assert sent == [job_id]
        assert _reload(TENANT_A, job_id).status == "pending", "補送不改狀態，worker 才認得它"

    def test_a_fresh_pending_job_is_left_alone(self, tenants: None, sent: list[uuid.UUID]) -> None:
        """門檻擋的是「正常也會短暫停留」——剛送出去的訊息還在飛。

        少了它，每一次重建都會在排隊的那幾秒內被多送一次。
        """
        _job(TENANT_A, status="pending", stale_minutes=0)

        assert StuckReindexRescueService().rescue_tenant(TENANT_A) == 0
        assert sent == []


class TestHalfDoneJobsAreFailedNotRequeued:
    @pytest.mark.parametrize("status", ["rechunking", "embedding"])
    def test_a_stale_running_job_is_marked_failed(
        self, tenants: None, sent: list[uuid.UUID], status: str
    ) -> None:
        """**不補送**：兩個 advance 併行會把同一批 chunk 各算一次（付兩次錢）。

        標成 failed 之後使用者可以重新發起，而已算好的向量留著——新的 job 會跳過
        它們（`chunks_without_embedding`），所以這條路徑一毛都不浪費。
        """
        job_id = _job(TENANT_A, status=status, stale_minutes=60)

        rescued = StuckReindexRescueService().rescue_tenant(TENANT_A)

        assert rescued == 1
        assert sent == [], "做到一半的 job 不得補送"
        stored = _reload(TENANT_A, job_id)
        assert stored.status == "failed"
        assert stored.error, "標成 failed 卻不寫原因的話，使用者只看得到一個沒有說明的失敗"
        assert stored.finished_at is not None

    def test_a_running_job_that_still_has_a_heartbeat_is_left_alone(
        self, tenants: None, sent: list[uuid.UUID]
    ) -> None:
        """重建期間有一段是**合法地什麼都不做**（重切之後等 ETL 把文件跑回 ready）。

        那段期間 job 靠每輪把 `updated_at` 推回去證明自己還活著。少了這一條，一個
        正常的大型重建會在門檻到期時被這支掃描器判死——而它其實跑得好好的。
        """
        _job(TENANT_A, status="rechunking", stale_minutes=1)

        assert StuckReindexRescueService().rescue_tenant(TENANT_A) == 0
        assert sent == []


class TestTerminalJobsAreNeverTouched:
    @pytest.mark.parametrize("status", ["completed", "failed"])
    def test_finished_jobs_are_out_of_scope(
        self, tenants: None, sent: list[uuid.UUID], status: str
    ) -> None:
        """終局狀態不論多老都不該被碰——`completed` 被重送會再切換一次。"""
        job_id = _job(TENANT_A, status=status, stale_minutes=60 * 24 * 30)

        assert StuckReindexRescueService().rescue_tenant(TENANT_A) == 0
        assert sent == []
        assert _reload(TENANT_A, job_id).status == status


class TestSweep:
    def test_rescue_all_walks_every_tenant(self, tenants: None, sent: list[uuid.UUID]) -> None:
        """逐租戶迴圈少了 context 會一列都掃不到（RLS fail closed）——而它照樣回 0。"""
        job_a = _job(TENANT_A, status="pending", stale_minutes=60)
        job_b = _job(TENANT_B, status="pending", stale_minutes=60)

        total = StuckReindexRescueService().rescue_all()

        assert total == 2
        assert set(sent) == {job_a, job_b}

    def test_one_tenants_failure_does_not_stop_the_round(
        self, tenants: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """單一租戶失敗不中斷整輪（同 `reconcile_all`／`purge_all`）。"""
        _job(TENANT_B, status="pending", stale_minutes=60)
        service = StuckReindexRescueService()
        original = service.rescue_tenant

        def _explode(tenant_id: uuid.UUID) -> int:
            if tenant_id == TENANT_A:
                raise RuntimeError("這個租戶壞掉了")
            return original(tenant_id)

        monkeypatch.setattr(service, "rescue_tenant", _explode)

        assert service.rescue_all() == 1


class TestHeartbeat:
    def test_progress_updates_bump_updated_at(self, tenants: None) -> None:
        """`updated_at` 是 `auto_now`，而 **`QuerySet.update()` 不會觸發它**。

        Repository 的 `update()` 因此必須自己補上時間戳。少了那一行，每一次進度回寫
        都不會讓 job 看起來「有動靜」，於是這支掃描器會把所有正在跑的重建判死——
        而那個錯誤的形狀是「重建到一半突然失敗」，看起來像 provider 的問題。
        """
        from repositories.knowledge import KbReindexJobRepository

        job_id = _job(TENANT_A, status="embedding", stale_minutes=60)
        before = _reload(TENANT_A, job_id).updated_at

        with tenant_scope(TENANT_A):
            KbReindexJobRepository().update(job_id, embedded_chunks=5)

        assert _reload(TENANT_A, job_id).updated_at > before

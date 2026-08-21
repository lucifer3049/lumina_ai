"""驗收：停滯文件的補償掃描（08 §2 狀態機的縫隙，1B/1C 遺留缺口，2A-2b 收尾）。

`enqueue_*` 是 best-effort：broker 掛掉時上傳照樣成功（1B-3 的決定，正確），但
文件會停在 `uploaded`（ingest 訊息丟了）或 `chunked`（embed 訊息丟了）——兩個
狀態都「看起來只是還在處理」，而沒有任何東西會再碰它們。`core/tasks.py` 的註解
從 1B 起就寫著「需 Celery Beat，排 2A」；Beat 現在有了，這是兌現。

只救**過期的**（停超過 `etl_stuck_after_seconds`）：新上傳的文件正常也會在
`uploaded` 待幾秒，立刻補送等於每份文件都送兩次——冪等擋得住重算，擋不住浪費。

三件事錯了都不會有例外：

1. **把正常處理中的文件再送一次**（門檻沒生效）。
2. **狀態與任務對錯邊**——uploaded 送去 embed 會被 service 的狀態防呆擋下然後
   安靜返回，文件繼續停著，而掃描器每輪都「成功」。
3. **漏租戶**（同對帳與清理的迴圈風險）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.knowledge.rescue import StuckDocumentRescueService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_document, make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)

_LONG_AGO = datetime.now(UTC) - timedelta(hours=2)


@pytest.fixture
def tenants() -> None:
    for tenant_id, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=slug)


def _stale_document(tenant_id: uuid.UUID, *, status: str) -> uuid.UUID:
    """一份停滯已久的文件（updated_at 在門檻之外）。"""
    with tenant_scope(tenant_id):
        kb = make_knowledge_base(tenant_id=tenant_id)
        document = make_document(kb=kb, status=status)
        # auto_now 蓋不掉建立值，用 queryset update 把它推回過去。
        type(document).objects.filter(id=document.id).update(updated_at=_LONG_AGO)
        return uuid.UUID(str(document.id))


def _fresh_document(tenant_id: uuid.UUID, *, status: str) -> uuid.UUID:
    with tenant_scope(tenant_id):
        kb = make_knowledge_base(tenant_id=tenant_id)
        return uuid.UUID(str(make_document(kb=kb, status=status).id))


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[uuid.UUID]]:
    """攔下兩種 enqueue，記錄哪些文件被補送。"""
    import services.knowledge.rescue as rescue

    calls: dict[str, list[uuid.UUID]] = {"ingest": [], "embed": []}

    def _capture(kind: str) -> Any:
        def _fake(**kwargs: Any) -> str:
            calls[kind].append(kwargs["document_id"])
            return "task-id"

        return _fake

    monkeypatch.setattr(rescue, "enqueue_ingestion", _capture("ingest"))
    monkeypatch.setattr(rescue, "enqueue_embedding", _capture("embed"))
    return calls


class TestRescue:
    def test_stale_uploaded_goes_back_to_ingest(
        self, tenants: None, sent: dict[str, list[uuid.UUID]]
    ) -> None:
        document_id = _stale_document(TENANT_A, status="uploaded")

        rescued = StuckDocumentRescueService().rescue_all()

        assert rescued == 1
        assert sent["ingest"] == [document_id]
        assert sent["embed"] == []

    def test_stale_chunked_goes_back_to_embedding(
        self, tenants: None, sent: dict[str, list[uuid.UUID]]
    ) -> None:
        document_id = _stale_document(TENANT_A, status="chunked")

        StuckDocumentRescueService().rescue_all()

        assert sent["embed"] == [document_id]
        assert sent["ingest"] == []

    def test_fresh_documents_are_left_alone(
        self, tenants: None, sent: dict[str, list[uuid.UUID]]
    ) -> None:
        """剛上傳的文件正常也在 uploaded——立刻補送等於每份都送兩次。"""
        _fresh_document(TENANT_A, status="uploaded")
        _fresh_document(TENANT_A, status="chunked")

        rescued = StuckDocumentRescueService().rescue_all()

        assert rescued == 0
        assert sent == {"ingest": [], "embed": []}

    def test_terminal_and_active_states_are_not_rescued(
        self, tenants: None, sent: dict[str, list[uuid.UUID]]
    ) -> None:
        """ready／failed 是終局；parsing／embedding 有 worker 在跑（那條線的斷裂
        由 acks_late 與重試管，不歸掃描器）。"""
        for status in ("ready", "failed", "parsing", "embedding"):
            _stale_document(TENANT_A, status=status)

        rescued = StuckDocumentRescueService().rescue_all()

        assert rescued == 0

    def test_every_tenant_is_scanned(self, tenants: None, sent: dict[str, list[uuid.UUID]]) -> None:
        a = _stale_document(TENANT_A, status="uploaded")
        b = _stale_document(TENANT_B, status="uploaded")

        StuckDocumentRescueService().rescue_all()

        assert set(sent["ingest"]) == {a, b}

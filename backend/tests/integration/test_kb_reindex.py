"""驗收：KB reindex 的四步與保留窗（06 §2.2、04 §4.4 的 `reembed_kb`，工作包 2B-6）。

`test_kb_reindex_planning.py` 驗的是判斷，這一檔驗的是**與 DB 的互動**——而四步流程
裡每一步的失敗模式都在 DB 那一側，假物件一項都驗不到：

1. **並存**靠 `UNIQUE(chunk, model, embedding_version)`（05 §3.2 那段 docstring 存在
   的全部理由）。少了它，重建期間只能「先刪再寫」，那幾十分鐘檢索什麼都查不到。
2. **原子切換**是一個交易寫三個欄位（model、embedding_version、indexed_knowledge_
   version）。分開寫的話，中間那一瞬間 KB 指向一個不存在的組合，檢索回零筆。
3. **隔離**：reindex 是跨整個 KB 的批次，少了 tenant context 它一列都讀不到
   （RLS fail closed）——症狀是「job 秒完成、0 個 chunk」，而狀態是成功。
4. **保留窗**：可回退的窗口從**切換**起算，而清理器要刪的是「舊版」不是「非現行版」
   ——重建期間新版還沒切換，那時的「非現行版」正是還在服務查詢的那一份。

provider 一律是假的（CLAUDE.md）：這裡驗的是編排，不是向量品質。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from ai.gateway import AIGateway
from ai.gateway.providers import ProviderEmbedding
from apps.knowledge.models import Chunk, Document, Embedding, KbReindexJob, KnowledgeBase
from core.exceptions import ConflictError, NotFoundError, ProviderError
from services.knowledge.cleanup import OldEmbeddingCleanupService
from services.knowledge.reindex import KbReindexJobView, KbReindexService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import (
    make_chunk,
    make_document,
    make_embedding,
    make_knowledge_base,
)

pytestmark = pytest.mark.django_db(transaction=True)

OLD_MODEL = "text-embedding-3-small"
NEW_MODEL = "gemini-embedding-2"


class _FakeProvider:
    """回定長向量的假 provider；記下每次被要求的模型名。"""

    name = "fake"

    def __init__(self) -> None:
        self.models: list[str] = []

    def embed(self, texts: list[str], *, model: str, timeout_seconds: float) -> ProviderEmbedding:
        self.models.append(model)
        from config.settings.app_settings import get_app_settings

        dimensions = get_app_settings().ai_embedding_dimensions
        return ProviderEmbedding(
            vectors=[[0.5] * dimensions for _ in texts], model=model, prompt_tokens=len(texts)
        )


def _service(provider: _FakeProvider | None = None) -> KbReindexService:
    return KbReindexService(gateway=AIGateway(embedding_provider=provider or _FakeProvider()))


@pytest.fixture
def tenants() -> None:
    for tenant_id, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=slug)


def _kb_with_ready_chunks(
    tenant_id: uuid.UUID, *, chunks: int = 3, knowledge_version: int = 1
) -> uuid.UUID:
    """一個可以被重建的 KB：一份 ready 文件、`chunks` 個現行 chunk，各一份舊版向量。"""
    with tenant_scope(tenant_id):
        kb = make_knowledge_base(
            tenant_id=tenant_id,
            embedding_model=OLD_MODEL,
            embedding_version=1,
            knowledge_version=knowledge_version,
        )
        document = make_document(kb=kb, status="ready")
        for seq in range(chunks):
            chunk = make_chunk(document=document, seq=seq, content=f"段落 {seq}")
            make_embedding(chunk=chunk, model=OLD_MODEL, embedding_version=1)
        return uuid.UUID(str(kb.id))


def _reload(tenant_id: uuid.UUID, kb_id: uuid.UUID) -> Any:
    with tenant_scope(tenant_id):
        return KnowledgeBase.objects.get(id=kb_id)


def _run(
    service: KbReindexService, tenant_id: uuid.UUID, job_id: uuid.UUID, *, limit: int = 20
) -> KbReindexJobView:
    """反覆 advance 直到 terminal——worker 做的就是這件事。"""
    for _ in range(limit):
        view = service.advance(tenant_id, job_id)
        if view.status in {"completed", "failed"}:
            return view
    raise AssertionError("reindex 沒有在合理的批次數內收斂")


class TestStepOneKeepsServingTheOldVersion:
    """第 1 步：**建立目標，但不動現行值**。"""

    def test_starting_does_not_touch_the_live_model_or_version(self, tenants: None) -> None:
        kb_id = _kb_with_ready_chunks(TENANT_A)

        job = _service().start(TENANT_A, kb_id, target_model=NEW_MODEL)

        kb = _reload(TENANT_A, kb_id)
        assert kb.embedding_model == OLD_MODEL, "新向量還沒算完，檢索必須繼續走舊的"
        assert kb.embedding_version == 1
        assert job.target_model == NEW_MODEL
        assert job.target_embedding_version == 2
        assert job.status == "pending"

    def test_the_job_records_how_much_work_there_is(self, tenants: None) -> None:
        """分母在開跑時定下來——邊跑邊算的話，進度會隨新上傳的文件倒退。"""
        kb_id = _kb_with_ready_chunks(TENANT_A, chunks=5)

        job = _service().start(TENANT_A, kb_id, target_model=NEW_MODEL)

        assert job.total_chunks == 5
        assert job.embedded_chunks == 0

    def test_a_second_run_on_the_same_kb_is_rejected(self, tenants: None) -> None:
        """兩個 job 會各自往同一批 chunk 寫不同的版本，然後互相把對方切掉。

        擋在 DB 的 partial unique（而不只是 service 的 if）：使用者連點兩次時，
        兩個請求會同時通過那個 if。
        """
        kb_id = _kb_with_ready_chunks(TENANT_A)
        service = _service()
        service.start(TENANT_A, kb_id, target_model=NEW_MODEL)

        with pytest.raises(ConflictError):
            service.start(TENANT_A, kb_id, target_model=NEW_MODEL)

    def test_a_finished_job_does_not_block_the_next_one(self, tenants: None) -> None:
        """約束的條件是「進行中」——換模型是會發生很多次的事。"""
        kb_id = _kb_with_ready_chunks(TENANT_A)
        service = _service()
        first = service.start(TENANT_A, kb_id, target_model=NEW_MODEL)
        _run(service, TENANT_A, first.id)

        second = service.start(TENANT_A, kb_id, target_model="third-model")

        assert second.id != first.id

    def test_another_tenants_kb_is_not_found(self, tenants: None) -> None:
        """404 而不是 403（09 §2.3）：回 403 等於承認那個 id 存在。"""
        kb_id = _kb_with_ready_chunks(TENANT_B)

        with pytest.raises(NotFoundError):
            _service().start(TENANT_A, kb_id, target_model=NEW_MODEL)


class TestStepTwoBuildsTheNewVersionAlongside:
    """第 2 步：新版向量算出來，**舊版一根汗毛都不能少**。"""

    def test_both_versions_exist_while_the_job_runs(self, tenants: None) -> None:
        kb_id = _kb_with_ready_chunks(TENANT_A, chunks=3)
        service = _service()
        job = service.start(TENANT_A, kb_id, target_model=NEW_MODEL)

        _run(service, TENANT_A, job.id)

        with tenant_scope(TENANT_A):
            assert Embedding.objects.filter(model=OLD_MODEL, embedding_version=1).count() == 3
            assert Embedding.objects.filter(model=NEW_MODEL, embedding_version=2).count() == 3

    def test_it_embeds_with_the_target_model_not_the_kbs_current_one(self, tenants: None) -> None:
        """讀 KB 現行值的話，這個 job 會把舊模型的向量再算一次（付錢買一份重複品）。"""
        kb_id = _kb_with_ready_chunks(TENANT_A)
        provider = _FakeProvider()
        service = _service(provider)
        job = service.start(TENANT_A, kb_id, target_model=NEW_MODEL)

        _run(service, TENANT_A, job.id)

        assert provider.models, "provider 沒有被呼叫過——這個 job 什麼都沒算"
        assert set(provider.models) == {NEW_MODEL}

    def test_superseded_chunks_are_not_re_embedded(self, tenants: None) -> None:
        """舊版 chunk 已經退出檢索（partial index），為它們算向量是純粹的浪費。"""
        kb_id = _kb_with_ready_chunks(TENANT_A, chunks=2)
        with tenant_scope(TENANT_A):
            document = Document.objects.get(kb_id=kb_id)
            make_chunk(document=document, seq=90, doc_version=1, superseded=True, content="舊")

        service = _service()
        job = service.start(TENANT_A, kb_id, target_model=NEW_MODEL)
        view = _run(service, TENANT_A, job.id)

        assert view.total_chunks == 2
        with tenant_scope(TENANT_A):
            assert Embedding.objects.filter(model=NEW_MODEL).count() == 2

    def test_progress_is_written_back_batch_by_batch(self, tenants: None) -> None:
        """進度只在最後寫的話，前端在整段重建期間看到的都是 0——與「卡住了」無法區分。"""
        kb_id = _kb_with_ready_chunks(TENANT_A, chunks=3)
        service = _service()
        job = service.start(TENANT_A, kb_id, target_model=NEW_MODEL)

        service.advance(TENANT_A, job.id)

        with tenant_scope(TENANT_A):
            stored = KbReindexJob.objects.get(id=job.id)
        assert stored.status in {"embedding", "completed"}
        assert stored.started_at is not None


class TestStepThreeSwitchesAtomically:
    """第 3 步：**唯一不可逆的一步**。"""

    def test_completion_switches_model_version_and_knowledge_version_together(
        self, tenants: None
    ) -> None:
        kb_id = _kb_with_ready_chunks(TENANT_A, knowledge_version=1)
        service = _service()
        job = service.start(TENANT_A, kb_id, target_model=NEW_MODEL)

        view = _run(service, TENANT_A, job.id)

        kb = _reload(TENANT_A, kb_id)
        assert view.status == "completed"
        assert (kb.embedding_model, kb.embedding_version) == (NEW_MODEL, 2)
        assert kb.indexed_knowledge_version == kb.knowledge_version, (
            "切換後「這個 KB 需要重建嗎」必須變回 False，否則畫面永遠提示要重建"
        )
        assert view.switched_at is not None

    def test_an_incomplete_job_never_switches(self, tenants: None) -> None:
        """provider 中途壞掉時，KB 必須留在舊版——切過去就是整庫回零筆。"""

        class _BrokenProvider(_FakeProvider):
            def embed(self, texts: list[str], *, model: str, timeout_seconds: float) -> Any:
                raise RuntimeError("provider 掛了")

        kb_id = _kb_with_ready_chunks(TENANT_A)
        service = _service(_BrokenProvider())
        job = service.start(TENANT_A, kb_id, target_model=NEW_MODEL)

        # Gateway 會把 adapter 的例外分類成 （重試耗盡之後），
        # 而不是原樣往上拋——這一層要驗的是「炸掉時 KB 沒有被切過去」。
        with pytest.raises(ProviderError):
            _run(service, TENANT_A, job.id)

        kb = _reload(TENANT_A, kb_id)
        assert (kb.embedding_model, kb.embedding_version) == (OLD_MODEL, 1)

    def test_the_old_vectors_survive_the_switch(self, tenants: None) -> None:
        """第 4 步之前不刪任何東西——「觀察期（可回退）」的可回退就是指這些向量。"""
        kb_id = _kb_with_ready_chunks(TENANT_A, chunks=3)
        service = _service()
        job = service.start(TENANT_A, kb_id, target_model=NEW_MODEL)

        _run(service, TENANT_A, job.id)

        with tenant_scope(TENANT_A):
            assert Embedding.objects.filter(model=OLD_MODEL, embedding_version=1).count() == 3


class TestRechunk:
    """切塊參數變了：重建的完整代價是「重切 + 重算」（本包範圍 B）。"""

    def test_a_config_change_makes_the_job_rechunk_first(self, tenants: None) -> None:
        kb_id = _kb_with_ready_chunks(TENANT_A, knowledge_version=2)

        job = _service().start(TENANT_A, kb_id)

        assert job.rechunk is True
        assert job.target_knowledge_version == 2
        assert job.total_documents == 1

    def test_each_document_gets_a_new_doc_version(self, tenants: None) -> None:
        """重切走既有的 re-ingest 路徑（`DocumentService.reingest`），不另寫一份。

        另寫一份的話，`(doc_id, doc_version, stage)` 的冪等鍵、`superseded` 的標記
        與 DLQ 的失敗分類就有兩份實作，而它們遲早會漂。
        """
        kb_id = _kb_with_ready_chunks(TENANT_A, knowledge_version=2)
        service = _service()
        job = service.start(TENANT_A, kb_id)

        service.advance(TENANT_A, job.id)

        with tenant_scope(TENANT_A):
            document = Document.objects.get(kb_id=kb_id)
        assert document.doc_version == 2
        # `start_new_version` 把狀態送回 ``uploaded``——重跑的起點與第一次上傳完全
        # 相同（`DocumentService.reingest` 的第 3 條）。本檔原本寫 ``parsing``，那是
        # 08 §2 狀態機上的下一站，不是 re-ingest 當下的落點。
        assert document.status == "uploaded"

    def test_it_does_not_switch_while_documents_are_still_reprocessing(self, tenants: None) -> None:
        """本檔開頭的第 3 個陷阱：重切完 ≠ 重建完。"""
        kb_id = _kb_with_ready_chunks(TENANT_A, knowledge_version=2)
        service = _service()
        job = service.start(TENANT_A, kb_id)

        view = service.advance(TENANT_A, job.id)

        assert view.status != "completed"
        kb = _reload(TENANT_A, kb_id)
        assert kb.indexed_knowledge_version == 1, "還沒重建完就不能宣稱已是新版本"

    def test_a_model_only_reindex_leaves_documents_alone(self, tenants: None) -> None:
        """換模型不重切：chunk 是文字，與用哪個模型算向量無關。"""
        kb_id = _kb_with_ready_chunks(TENANT_A, knowledge_version=1)
        service = _service()
        job = service.start(TENANT_A, kb_id, target_model=NEW_MODEL)

        _run(service, TENANT_A, job.id)

        with tenant_scope(TENANT_A):
            document = Document.objects.get(kb_id=kb_id)
            assert document.doc_version == 1
            assert Chunk.objects.filter(kb_id=kb_id, superseded=True).count() == 0


class TestStepFourPurgesAfterTheWindow:
    """第 4 步：觀察期過了才刪舊版向量。"""

    def _switched_kb(self, tenant_id: uuid.UUID, *, switched_days_ago: int) -> uuid.UUID:
        kb_id = _kb_with_ready_chunks(tenant_id, chunks=2)
        service = _service()
        job = service.start(tenant_id, kb_id, target_model=NEW_MODEL)
        _run(service, tenant_id, job.id)
        with tenant_scope(tenant_id):
            KbReindexJob.objects.filter(id=job.id).update(
                switched_at=timezone.now() - timedelta(days=switched_days_ago)
            )
        return kb_id

    def test_inside_the_window_nothing_is_deleted(self, tenants: None) -> None:
        """可回退 = 舊向量還在。窗內就刪的話，「回退」只剩「再重建一次」。"""
        self._switched_kb(TENANT_A, switched_days_ago=0)

        purged = OldEmbeddingCleanupService().purge_switched(TENANT_A)

        assert purged == 0
        with tenant_scope(TENANT_A):
            assert Embedding.objects.filter(model=OLD_MODEL).count() == 2

    def test_after_the_window_only_the_old_version_goes(self, tenants: None) -> None:
        self._switched_kb(TENANT_A, switched_days_ago=90)

        purged = OldEmbeddingCleanupService().purge_switched(TENANT_A)

        assert purged == 2
        with tenant_scope(TENANT_A):
            assert Embedding.objects.filter(model=OLD_MODEL).count() == 0
            assert Embedding.objects.filter(model=NEW_MODEL, embedding_version=2).count() == 2

    def test_a_kb_that_is_mid_reindex_is_left_alone(self, tenants: None) -> None:
        """重建期間的「非現行版」正是還在服務查詢的那一份（本檔開頭第 4 個陷阱）。

        清理器若以「不等於 KB 現行值」為條件，它會刪掉**新算好但還沒切換**的那一批，
        於是那個 job 永遠到不了 100%——而它每一輪都重算、每一輪都被刪。
        """
        kb_id = _kb_with_ready_chunks(TENANT_A, chunks=2)
        service = _service()
        job = service.start(TENANT_A, kb_id, target_model=NEW_MODEL)
        service.advance(TENANT_A, job.id)

        purged = OldEmbeddingCleanupService().purge_switched(TENANT_A)

        assert purged == 0
        with tenant_scope(TENANT_A):
            assert Embedding.objects.filter(model=OLD_MODEL).count() == 2

    def test_purge_all_walks_tenants_in_isolation(self, tenants: None) -> None:
        """逐租戶的維運迴圈少了 context 會一列都刪不到（RLS fail closed）。"""
        self._switched_kb(TENANT_A, switched_days_ago=90)
        self._switched_kb(TENANT_B, switched_days_ago=90)

        total = OldEmbeddingCleanupService().purge_all()

        assert total == 4
        for tenant_id in (TENANT_A, TENANT_B):
            with tenant_scope(tenant_id):
                assert Embedding.objects.filter(model=OLD_MODEL).count() == 0


class TestIsolation:
    def test_a_job_only_touches_its_own_tenants_data(self, tenants: None) -> None:
        kb_a = _kb_with_ready_chunks(TENANT_A, chunks=2)
        _kb_with_ready_chunks(TENANT_B, chunks=2)
        service = _service()

        job = service.start(TENANT_A, kb_a, target_model=NEW_MODEL)
        _run(service, TENANT_A, job.id)

        with tenant_scope(TENANT_B):
            assert Embedding.objects.filter(model=NEW_MODEL).count() == 0
            assert KbReindexJob.objects.count() == 0

    def test_latest_does_not_leak_across_tenants(self, tenants: None) -> None:
        kb_b = _kb_with_ready_chunks(TENANT_B)
        _service().start(TENANT_B, kb_b, target_model=NEW_MODEL)

        with pytest.raises(NotFoundError):
            _service().latest(TENANT_A, kb_b)

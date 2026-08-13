"""驗收：ETL 狀態機、冪等與 chunks 落地（08 §2/§6、13 §3 工作包 1B-6）。

這一層把前面幾包串起來：物件儲存的位元組 → Extract → Clean → Chunk → chunks 落 DB，
每個階段留下一筆 `EtlJob`。放 integration 而不是 unit，因為**要驗的東西正是與 DB 和
物件儲存的互動**：冪等靠唯一約束、隔離靠 RLS，兩者都無法用假物件驗證。

四件事非驗不可，每一件都對應一種「重跑之後資料靜默壞掉」的情況：

1. **狀態機推進**——文件的 status 與各階段的 job 狀態要一致。不一致時前端顯示的
   進度與實際情況無關，而使用者只會看到一份永遠「處理中」的文件。
2. **冪等**（08 §6 的 `(doc_id, doc_version, stage)`）——Celery 的 at-least-once 保證
   代表同一個任務**一定會**有重送的一天。重跑若不是安全的，症狀是同一份文件的 chunk
   在檢索結果裡出現兩次。
3. **失敗是結構化的**——stage、原因、可否重跑都要寫進 `document.error`，那是 DLQ
   與使用者看到的東西。只寫「處理失敗」的話，維運分不出毒檔與我們的 bug。
4. **租戶**——chunk 與 job 都帶 tenant_id，且跨租戶取不到文件。
"""

from __future__ import annotations

import uuid

import pytest

from apps.knowledge.models import Chunk, Document, EtlJob
from core.exceptions import NotFoundError
from services.knowledge.documents import DocumentService
from services.knowledge.ingestion import IngestionService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)

_MARKDOWN = """# 第一章 總則

本章說明適用範圍，內容足夠長以便產生至少一個 chunk。

## 第一節 定義

本節定義名詞。

| 項目 | 數值 |
| --- | --- |
| 延遲 | 300ms |
""".encode()


@pytest.fixture
def tenants() -> None:
    """兩個租戶（隔離斷言的載具）。

    走 ``tenant_scope`` 而不是 ``tenant_context``：建立資料要通過 RLS 的 ``WITH
    CHECK``，而 policy 讀的是**交易區域參數**——只設 contextvar 的話 INSERT 會被
    擋下，或 SELECT 靜默回空集合（症狀是 ``Tenant matching query does not exist``）。
    """
    for tenant_id, name in ((TENANT_A, "a"), (TENANT_B, "b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=f"tenant-{name}")


def _upload(
    tenant_id: uuid.UUID, *, content: bytes = _MARKDOWN, filename: str = "guide.md"
) -> uuid.UUID:
    with tenant_scope(tenant_id):
        kb = make_knowledge_base(tenant_id=tenant_id)
    view = DocumentService().upload(tenant_id, kb.id, filename=filename, content=content)
    return view.id


class TestHappyPath:
    def test_chunks_are_written_with_content_and_metadata(self, tenants: None) -> None:
        document_id = _upload(TENANT_A)

        IngestionService().ingest(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            chunks = list(Chunk.objects.filter(document_id=document_id).order_by("seq"))
        assert chunks, "沒有產生任何 chunk"
        assert [chunk.seq for chunk in chunks] == list(range(len(chunks)))
        assert all(chunk.token_count > 0 for chunk in chunks)
        assert any("第一章 總則" in chunk.content for chunk in chunks)

    def test_heading_path_survives_into_chunk_meta(self, tenants: None) -> None:
        """引用要能說出「出自哪一節」——這是整條鏈唯一會掉資訊的地方。"""
        document_id = _upload(TENANT_A)

        IngestionService().ingest(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            chunks = list(Chunk.objects.filter(document_id=document_id).order_by("seq"))
        assert any(chunk.meta.get("heading_path") for chunk in chunks)

    def test_document_reaches_the_chunked_state(self, tenants: None) -> None:
        """1B 的終點是 ``chunked``：``ready`` 要等 1C 的 embedding（08 §2 的狀態機）。

        提前把它標成 ready 的話，1C 之前的檢索會查到一份「宣稱可用、實際沒有向量」
        的文件——而那時失敗的是問答，不是這裡。
        """
        document_id = _upload(TENANT_A)

        IngestionService().ingest(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            document = Document.objects.get(id=document_id)
        assert document.status == "chunked"
        assert document.error is None

    def test_every_stage_leaves_a_job_row(self, tenants: None) -> None:
        document_id = _upload(TENANT_A)

        IngestionService().ingest(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            jobs = {job.stage: job for job in EtlJob.objects.filter(document_id=document_id)}
        assert set(jobs) == {"extract", "clean", "chunk"}
        assert all(job.status == "succeeded" for job in jobs.values())
        assert all(job.finished_at is not None for job in jobs.values())

    def test_quality_stats_are_recorded(self, tenants: None) -> None:
        """08 §4 的 stats：block 數、丟棄率、chunk 數、平均 token 都要落地。

        它們是「這份來源讀得好不好」唯一的事後證據——沒有它們，品質問題只能靠
        使用者回報「答得很爛」。
        """
        document_id = _upload(TENANT_A)

        IngestionService().ingest(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            chunk_job = EtlJob.objects.get(document_id=document_id, stage="chunk")
            clean_job = EtlJob.objects.get(document_id=document_id, stage="clean")
        assert chunk_job.stats["chunk_count"] > 0
        assert chunk_job.stats["avg_tokens"] > 0
        assert "drop_rate" in clean_job.stats
        assert clean_job.stats["language"] == "zh"


class TestIdempotency:
    def test_running_twice_does_not_duplicate_chunks(self, tenants: None) -> None:
        """Celery 是 at-least-once：同一個任務**一定會**有重送的一天。

        重跑不安全的話，症狀是同一段內容在檢索結果裡出現兩次——而那不會有錯誤訊息。
        """
        document_id = _upload(TENANT_A)
        service = IngestionService()

        first = service.ingest(TENANT_A, document_id)
        second = service.ingest(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            count = Chunk.objects.filter(document_id=document_id).count()
        assert first.chunk_count == second.chunk_count == count

    def test_the_second_run_reuses_the_existing_jobs(self, tenants: None) -> None:
        """冪等鍵是 ``(doc_id, doc_version, stage)``——重跑不得產生第二組 job 列。"""
        document_id = _upload(TENANT_A)
        service = IngestionService()

        service.ingest(TENANT_A, document_id)
        service.ingest(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            assert EtlJob.objects.filter(document_id=document_id).count() == 3


class TestFailures:
    def test_a_poisoned_document_fails_with_a_structured_error(self, tenants: None) -> None:
        """壞檔 → status=failed，且 error 帶 stage / cause / 可否重跑（08 §6 的 DLQ 內容）。"""
        document_id = _upload(
            TENANT_A, content=b"%PDF-1.7\nnot really a pdf", filename="broken.pdf"
        )

        IngestionService().ingest(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            document = Document.objects.get(id=document_id)
            job = EtlJob.objects.get(document_id=document_id, stage="extract")
        assert document.status == "failed"
        assert document.error is not None
        assert document.error["stage"] == "extract"
        assert document.error["cause"]
        assert document.error["retryable"] is False
        assert job.status == "failed"

    def test_a_failed_document_produces_no_chunks(self, tenants: None) -> None:
        """失敗的文件不得留下半套 chunk——那會被檢索到，而它的來源已經標成 failed。"""
        document_id = _upload(
            TENANT_A, content=b"%PDF-1.7\nnot really a pdf", filename="broken.pdf"
        )

        IngestionService().ingest(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            assert Chunk.objects.filter(document_id=document_id).count() == 0


class TestTenantIsolation:
    def test_chunks_and_jobs_carry_the_tenant(self, tenants: None) -> None:
        document_id = _upload(TENANT_A)

        IngestionService().ingest(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            assert all(
                chunk.tenant_id == TENANT_A
                for chunk in Chunk.objects.filter(document_id=document_id)
            )
            assert all(
                job.tenant_id == TENANT_A for job in EtlJob.objects.filter(document_id=document_id)
            )

    def test_another_tenant_cannot_ingest_the_document(self, tenants: None) -> None:
        """跨租戶取不到文件——回 404 語意的 `NotFoundError`，不是「處理失敗」。

        ETL 由 worker 觸發，租戶來自任務參數而不是請求；參數錯了就是程式錯誤，
        必須在碰到任何資料之前停下來。
        """
        document_id = _upload(TENANT_A)

        with pytest.raises(NotFoundError):
            IngestionService().ingest(TENANT_B, document_id)

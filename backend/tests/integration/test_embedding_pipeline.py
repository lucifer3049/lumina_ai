"""驗收：embedding worker（06 §2·§2.1、08 §2 的狀態機、13 §3 工作包 1C-3）。

1C-2 已經證明向量存得進去、索引建得對。這一包要證明的是**誰在什麼時候把它們算出來**，
而這條路上每一種錯誤的症狀都不是例外，是「檢索少了東西」或「帳單多了一筆」：

1. **狀態機走到 `ready`**（08 §2：chunked → embedding → ready）。1B 的終點是 `chunked`，
   而 1D 的檢索只認 `ready`。少了這一段，文件會永遠停在 chunked 而沒有任何錯誤——
   使用者看到的是一份「處理完了但問不到」的文件。
2. **冪等 = 不重算**。Celery 是 at-least-once，而這裡的重跑不像 chunk 那樣只是浪費
   CPU：每個 chunk 是一次真的 API 呼叫，重算一份 500 頁的 PDF 就是重付一次錢。
   `chunks_without_embedding`（1C-2 已備）是這件事唯一的依據。
3. **批次的部分失敗不能全丟**。第 7 批炸掉時前 6 批的向量必須留著，否則一份大文件在
   provider 不穩的那段時間會永遠跑不完——每次重跑都從第 1 批開始，而它每次都會在某個
   地方再炸一次。
4. **失敗分類**（08 §6）。配額用盡與模型未啟用重試幾次都一樣（永久失敗）；429 與逾時
   要往上拋讓 Celery 退避。判錯的代價是「把同一個結論做四遍」或「把可回復的狀況寫成
   毒檔」，而後者要人工介入才回得來。

放 integration 而不是 unit：要驗的正是與 DB 的互動（冪等靠唯一約束、隔離靠 RLS、
superseded 靠 partial index 的條件），假物件驗不到其中任何一項。**provider 一律是假的**
（CLAUDE.md）——這裡驗的是機制，不是向量品質。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ai.gateway import AIGateway
from ai.gateway.providers import ProviderEmbedding
from apps.knowledge.models import Document, Embedding, EtlJob
from core.exceptions import (
    ModelNotEnabledError,
    NotFoundError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from services.knowledge.documents import DocumentService
from services.knowledge.embedding import EMBED_BATCH_SIZE, EmbeddingService
from services.knowledge.ingestion import IngestionService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_chunk, make_document, make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)

MODEL = "mock-embedding"

_MARKDOWN = """# 第一章 總則

本章說明適用範圍，內容足夠長以便產生至少一個 chunk。

## 第一節 定義

本節定義名詞。
""".encode()


class _CountingProvider:
    """記錄每一次呼叫送了哪些文字的假 provider。

    **計次是這一層最重要的斷言**：冪等在 ETL 只是「別寫兩次」，在這裡是「別付兩次
    錢」。沒有這個計數器，重算整份文件的測試會全綠——結果完全正確，只是貴了一倍。
    """

    name = "counting"

    def __init__(self, *, failures: list[Exception | None] | None = None) -> None:
        self.calls: list[list[str]] = []
        # 公開且可變：續跑的測試要在第一輪失敗之後「把 provider 修好」再跑一次。
        self.failures: list[Exception | None] = list(failures or [])

    @property
    def embedded_texts(self) -> list[str]:
        return [text for call in self.calls for text in call]

    def embed(self, texts: list[str], *, model: str, timeout_seconds: float) -> ProviderEmbedding:
        self.calls.append(list(texts))
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        from config.settings.app_settings import get_app_settings

        dimensions = get_app_settings().ai_embedding_dimensions
        return ProviderEmbedding(
            vectors=[[0.1] * dimensions for _ in texts],
            model=model,
            prompt_tokens=sum(len(text) for text in texts),
        )


def _service(provider: _CountingProvider) -> EmbeddingService:
    """注入假 provider 的 service。

    ``retry_backoff_seconds=()``：Gateway 的退避是 1s/2s，而失敗路徑的測試有好幾條
    ——照實等會讓這個檔案多花十幾秒，而那些秒數驗不到任何東西（退避本身已由
    tests/unit/test_ai_gateway.py 釘住）。
    """
    return EmbeddingService(
        gateway=AIGateway(embedding_provider=provider, retry_backoff_seconds=())
    )


@pytest.fixture
def tenants() -> None:
    """兩個租戶（隔離斷言的載具）。走 ``tenant_scope`` 的理由見 test_ingestion.py。"""
    for tenant_id, name in ((TENANT_A, "a"), (TENANT_B, "b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=f"tenant-{name}")


def _ingested(tenant_id: uuid.UUID) -> uuid.UUID:
    """跑完 1B 的完整 ETL，回傳停在 ``chunked`` 的文件 id。"""
    with tenant_scope(tenant_id):
        kb = make_knowledge_base(tenant_id=tenant_id)
    view = DocumentService().upload(tenant_id, kb.id, filename="guide.md", content=_MARKDOWN)
    IngestionService().ingest(tenant_id, view.id)
    return view.id


def _chunked_document(tenant_id: uuid.UUID, *, chunk_count: int, **kb_fields: Any) -> uuid.UUID:
    """直接造一份已切塊的文件（不跑 ETL）。

    批次與冪等的斷言需要幾十上百個 chunk，而真的解析出那麼多內容要一份很大的來源檔
    ——那讓測試慢，且慢的部分與這一包無關。
    """
    with tenant_scope(tenant_id):
        kb = make_knowledge_base(tenant_id=tenant_id, **kb_fields)
        document = make_document(kb=kb, status="chunked")
        for seq in range(chunk_count):
            make_chunk(document=document, seq=seq, content=f"第 {seq} 段內容")
        return uuid.UUID(str(document.id))


class TestHappyPath:
    def test_every_active_chunk_gets_a_vector(self, tenants: None) -> None:
        provider = _CountingProvider()
        document_id = _chunked_document(TENANT_A, chunk_count=3)

        _service(provider).embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            vectors = Embedding.objects.filter(chunk__document_id=document_id)
            assert vectors.count() == 3
            assert all(len(row.vector) > 0 for row in vectors)

    def test_the_document_reaches_ready(self, tenants: None) -> None:
        """08 §2 的終點。1B 停在 ``chunked``，補上這一段之後整條寫路徑才走得完。

        提前標 ready（在向量寫完之前）的話，1D 的檢索會查到一份「宣稱可用、實際沒有
        向量」的文件——而那時失敗的是問答，不是這裡。
        """
        document_id = _chunked_document(TENANT_A, chunk_count=3)

        _service(_CountingProvider()).embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            document = Document.objects.get(id=document_id)
        assert document.status == "ready"
        assert document.error is None

    def test_the_embed_stage_leaves_a_job_row(self, tenants: None) -> None:
        """`etl_jobs` 是維運看的細節，而 embed 與前三個階段共用同一組冪等鍵。

        stats 要帶用量：**token 數是 2A 計費的原料**，而它只有在呼叫的當下拿得到。
        漏記的話租戶成本會被低估，而低估不會有人回報。
        """
        document_id = _chunked_document(TENANT_A, chunk_count=3)

        _service(_CountingProvider()).embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            job = EtlJob.objects.get(document_id=document_id, stage="embed")
        assert job.status == "succeeded"
        assert job.finished_at is not None
        assert job.stats["embedded_count"] == 3
        assert job.stats["prompt_tokens"] > 0

    def test_the_model_and_version_come_from_the_knowledge_base(self, tenants: None) -> None:
        """模型與版本是 **KB 的屬性**（06 §2.2），不是全域常數。

        重嵌入的做法是「新版本算完 → 原子切換」，那要求同一個 chunk 能同時有兩個版本
        的向量。寫死全域值的話，切換就只剩「先刪再寫」，而那幾分鐘檢索什麼都查不到。
        """
        document_id = _chunked_document(
            TENANT_A, chunk_count=2, embedding_model="custom-embedding", embedding_version=3
        )

        _service(_CountingProvider()).embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            rows = list(Embedding.objects.filter(chunk__document_id=document_id))
        assert rows
        assert all(row.model == "custom-embedding" for row in rows)
        assert all(row.embedding_version == 3 for row in rows)

    def test_an_unset_knowledge_base_model_falls_back_to_settings(self, tenants: None) -> None:
        """KB 沒指定模型時用設定的預設值——**不是空字串**。

        空字串會照樣寫進 `UNIQUE(chunk_id, model, embedding_version)`，於是那批向量
        永遠對不上任何一次檢索（檢索會用設定值去找），而資料看起來完全正常。
        """
        from config.settings.app_settings import get_app_settings

        document_id = _chunked_document(TENANT_A, chunk_count=1, embedding_model="")

        _service(_CountingProvider()).embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            row = Embedding.objects.get(chunk__document_id=document_id)
        assert row.model == get_app_settings().ai_embedding_model

    def test_the_full_pipeline_reaches_ready(self, tenants: None) -> None:
        """上傳 → ETL → embedding 一路走到底（本包對 Phase 1 DoD 的貢獻）。

        前面幾條用造出來的 chunk 跑得快，但它們驗不到「1B 產出的形狀正好是 1C 吃得下
        的形狀」——例如 chunk 內容為空、token_count 為 0 這類只有真的切塊才會出現的
        邊界。
        """
        provider = _CountingProvider()
        document_id = _ingested(TENANT_A)

        _service(provider).embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            document = Document.objects.get(id=document_id)
            count = Embedding.objects.filter(chunk__document_id=document_id).count()
        assert document.status == "ready"
        assert count > 0
        assert len(provider.embedded_texts) == count


class TestIdempotency:
    def test_a_second_run_does_not_call_the_provider(self, tenants: None) -> None:
        """**重跑不得重算**——每個 chunk 是一次真的 API 呼叫。

        ETL 的重跑只浪費 CPU，這裡浪費的是錢，而帳單上看不出哪一筆是重複的。
        """
        provider = _CountingProvider()
        document_id = _chunked_document(TENANT_A, chunk_count=3)
        service = _service(provider)

        service.embed_document(TENANT_A, document_id)
        calls_after_first = len(provider.calls)
        service.embed_document(TENANT_A, document_id)

        assert len(provider.calls) == calls_after_first

    def test_a_second_run_leaves_one_vector_per_chunk(self, tenants: None) -> None:
        document_id = _chunked_document(TENANT_A, chunk_count=3)
        service = _service(_CountingProvider())

        service.embed_document(TENANT_A, document_id)
        service.embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            assert Embedding.objects.filter(chunk__document_id=document_id).count() == 3

    def test_only_the_chunks_without_a_vector_are_sent(self, tenants: None) -> None:
        """半途中斷後續跑：已經有向量的 chunk 不再送。

        沒有這條，一份在第 90% 掛掉的大文件每次重跑都要從頭付一次錢——而它掛掉的
        原因（provider 不穩）通常還在。
        """
        # 第二批丟一個 provider SDK 的原生例外——Gateway 會把它收斂成
        # `ProviderUnavailableError`（可重試），那是「provider 暫時壞掉」的形狀。
        provider = _CountingProvider(failures=[None, RuntimeError("connection reset")])
        document_id = _chunked_document(TENANT_A, chunk_count=EMBED_BATCH_SIZE + 5)
        service = _service(provider)

        with pytest.raises(ProviderUnavailableError):
            service.embed_document(TENANT_A, document_id)

        provider.failures.clear()  # provider 恢復正常
        provider.calls.clear()  # 只看第二輪送了什麼
        service.embed_document(TENANT_A, document_id)

        # 第二輪只補第一輪沒寫成功的那 5 筆——不是重送 69 筆。
        assert len(provider.embedded_texts) == 5
        with tenant_scope(TENANT_A):
            assert (
                Embedding.objects.filter(chunk__document_id=document_id).count()
                == EMBED_BATCH_SIZE + 5
            )

    def test_the_job_row_is_reused_across_runs(self, tenants: None) -> None:
        """冪等鍵仍是 ``(doc_id, doc_version, stage)``——重跑不得產生第二筆 job。"""
        document_id = _chunked_document(TENANT_A, chunk_count=2)
        service = _service(_CountingProvider())

        service.embed_document(TENANT_A, document_id)
        service.embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            assert EtlJob.objects.filter(document_id=document_id, stage="embed").count() == 1


class TestBatching:
    def test_chunks_are_sent_in_bounded_batches(self, tenants: None) -> None:
        """批次上限 64（06 §2.1）。

        不分批的話，一份 500 頁的 PDF 會變成一次帶上千段文字的請求——provider 會以
        413 或 token 上限退回整批，而症狀是「大文件永遠處理不完、小文件都正常」。
        """
        provider = _CountingProvider()
        document_id = _chunked_document(TENANT_A, chunk_count=EMBED_BATCH_SIZE * 2 + 3)

        _service(provider).embed_document(TENANT_A, document_id)

        assert len(provider.calls) == 3
        assert all(len(call) <= EMBED_BATCH_SIZE for call in provider.calls)

    def test_an_early_batch_survives_a_later_failure(self, tenants: None) -> None:
        """第 2 批炸掉時，第 1 批的向量必須已經在 DB 裡。

        整份文件包在一個交易裡的話，provider 在後段不穩會讓前段的成功一起回滾——
        每次重跑都從第 1 批開始，而它每次都會在某處再炸一次。大文件因此永遠跑不完。
        """
        provider = _CountingProvider(failures=[None, ProviderTimeoutError("provider 逾時")])
        document_id = _chunked_document(TENANT_A, chunk_count=EMBED_BATCH_SIZE + 5)

        with pytest.raises(ProviderTimeoutError):
            _service(provider).embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            assert (
                Embedding.objects.filter(chunk__document_id=document_id).count() == EMBED_BATCH_SIZE
            )

    def test_a_document_without_chunks_reaches_ready_without_calling_the_provider(
        self, tenants: None
    ) -> None:
        """零 chunk 的文件（空檔、只有圖的 PDF）是**成功**，不是失敗。

        標成 failed 的話使用者會看到一個沒有東西可修的錯誤；而打一次空的 provider
        呼叫則是白付一次延遲與費用。
        """
        provider = _CountingProvider()
        document_id = _chunked_document(TENANT_A, chunk_count=0)

        _service(provider).embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            document = Document.objects.get(id=document_id)
        assert document.status == "ready"
        assert provider.calls == []


class TestSupersededChunks:
    def test_superseded_chunks_are_not_embedded(self, tenants: None) -> None:
        """re-ingest 之後，舊版 chunk 不再算向量。

        它們即將被清理 job 硬刪（2A），而在那之前算一次是純粹的浪費——一份重跑三次
        的大文件會付四份錢，其中三份的資料在幾分鐘後就被刪掉。
        """
        provider = _CountingProvider()
        document_id = _ingested(TENANT_A)
        DocumentService().reingest(TENANT_A, document_id)
        IngestionService().ingest(TENANT_A, document_id)

        _service(provider).embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            active = list(
                Embedding.objects.filter(
                    chunk__document_id=document_id, chunk__superseded=False
                ).values_list("chunk_id", flat=True)
            )
            stale = Embedding.objects.filter(
                chunk__document_id=document_id, chunk__superseded=True
            ).count()
        assert active
        assert stale == 0


class TestFailureClassification:
    """判錯的代價：把可回復的狀況寫成毒檔（要人工介入），或把確定的結論做四遍。"""

    def test_a_rate_limit_propagates_for_celery_to_retry(self, tenants: None) -> None:
        """429 是**可重試**的：稍後再送就會成功。

        往上拋讓 Celery 走 08 §6 的退避（30s/2m/10m）。在這裡吞掉的話，任務會被當成
        處理完畢，而文件停在 embedding 且沒有人再碰它。
        """
        provider = _CountingProvider(failures=[ProviderRateLimitedError("配額尖峰")])
        document_id = _chunked_document(TENANT_A, chunk_count=2)

        with pytest.raises(ProviderRateLimitedError):
            _service(provider).embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            document = Document.objects.get(id=document_id)
        # 還會重試——這時就標 failed 的話，使用者會看到一個十分鐘後自己好掉的錯誤。
        assert document.status != "failed"

    def test_a_disabled_model_fails_permanently(self, tenants: None) -> None:
        """模型未啟用重試幾次都一樣 → 永久失敗，且錯誤要說得出卡在哪一階段。"""
        provider = _CountingProvider(failures=[ModelNotEnabledError(model="unlisted-embedding")])
        document_id = _chunked_document(TENANT_A, chunk_count=2)

        result = _service(provider).embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            document = Document.objects.get(id=document_id)
            job = EtlJob.objects.get(document_id=document_id, stage="embed")
        assert result.status == "failed"
        assert document.status == "failed"
        assert document.error is not None
        assert document.error["stage"] == "embed"
        assert document.error["retryable"] is False
        assert job.status == "failed"

    def test_retries_exhausted_records_a_retryable_failure(self, tenants: None) -> None:
        """重試耗盡要落在 DB（08 §6 的 DLQ 內容），且與毒檔分得開。

        ``retryable=True`` 的處置是「修環境後重跑」，``False`` 是「這份文件本身不行」。
        混成同一個狀態時，維運面對一排 failed 文件無從判斷該修什麼。
        """
        document_id = _chunked_document(TENANT_A, chunk_count=2)

        _service(_CountingProvider()).mark_retries_exhausted(
            TENANT_A, document_id, ProviderTimeoutError("provider 逾時"), attempts=3
        )

        with tenant_scope(TENANT_A):
            document = Document.objects.get(id=document_id)
        assert document.status == "failed"
        assert document.error is not None
        assert document.error["stage"] == "embed"
        assert document.error["retryable"] is True
        assert document.error["attempts"] == 3

    def test_a_third_party_message_does_not_reach_the_tenant(self, tenants: None) -> None:
        """第三方例外的訊息不進 ``document.error``（鐵則 9）。

        這份 dict 會經 `DocumentOut.error` 回到租戶手上，而 provider SDK 的錯誤字串
        常夾 endpoint、組織 id 與 API key 前綴。型別名留著——重試判定與統計要用它，
        而它不洩漏內容。
        """
        document_id = _chunked_document(TENANT_A, chunk_count=2)

        _service(_CountingProvider()).mark_retries_exhausted(
            TENANT_A,
            document_id,
            OSError("connect to https://api.vendor.internal org=acme key=sk-abc123"),
            attempts=3,
        )

        with tenant_scope(TENANT_A):
            document = Document.objects.get(id=document_id)
        assert document.error is not None
        assert document.error["cause"] == "OSError"
        assert "sk-abc123" not in document.error["message"]
        assert "vendor.internal" not in document.error["message"]


class TestChaining:
    def test_a_chunked_document_is_handed_to_the_embedding_queue(
        self, tenants: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ETL 跑完要**自動**把文件交給 embedding 佇列（06 §2 的 Q2）。

        少了這一步，整條鏈在 chunked 斷掉而沒有任何錯誤：訊息沒有人送、佇列是空的、
        API 回應完全正常，只有文件永遠不會變成 ready。這與 1B-6 漏掉 `make start` 的
        worker 是同一種失敗——**沒有人會收到通知**。
        """
        from services.knowledge import ingestion

        sent: list[tuple[uuid.UUID, uuid.UUID]] = []

        def _record(*, tenant_id: uuid.UUID, document_id: uuid.UUID) -> str:
            sent.append((tenant_id, document_id))
            return "task-id"

        monkeypatch.setattr(ingestion, "enqueue_embedding", _record)
        document_id = _ingested(TENANT_A)

        assert sent == [(TENANT_A, document_id)]

    def test_a_failed_document_is_not_handed_over(
        self, tenants: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """毒檔不排 embedding：那份文件沒有 chunk，送過去只會空跑一趟，而 DLQ 會多
        一筆看起來像故障的紀錄。"""
        from services.knowledge import ingestion

        sent: list[uuid.UUID] = []

        def _record(*, tenant_id: uuid.UUID, document_id: uuid.UUID) -> str:
            sent.append(document_id)
            return "task-id"

        monkeypatch.setattr(ingestion, "enqueue_embedding", _record)
        with tenant_scope(TENANT_A):
            kb = make_knowledge_base(tenant_id=TENANT_A)
        view = DocumentService().upload(
            TENANT_A, kb.id, filename="broken.pdf", content=b"%PDF-1.7\nnot really a pdf"
        )

        IngestionService().ingest(TENANT_A, view.id)

        assert sent == []


class TestStaleTasks:
    """Celery 的訊息可能比它描述的世界舊——重送、退避、re-ingest 都會造成落差。"""

    def test_a_task_for_a_reingested_document_changes_nothing(self, tenants: None) -> None:
        """re-ingest 之後，舊訊息不得把文件標成 ready。

        文件在 chunked 時排了 embedding，使用者接著按 re-ingest（chunked 是允許重跑
        的），於是 doc_version 變成 2 而新版的 chunk 還沒切出來。舊訊息這時進來會看到
        「這一版沒有 chunk 要算」——照著做的結果是一份**零向量但狀態是 ready** 的
        文件，而檢索查得到它、它什麼都答不出來。
        """
        provider = _CountingProvider()
        document_id = _ingested(TENANT_A)
        DocumentService().reingest(TENANT_A, document_id)  # → uploaded、doc_version=2

        result = _service(provider).embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            document = Document.objects.get(id=document_id)
        assert document.status == "uploaded", "舊訊息不得推進狀態"
        assert result.embedded_count == 0
        assert provider.calls == []

    def test_a_redelivered_task_on_a_ready_document_is_harmless(self, tenants: None) -> None:
        """已經 ready 的文件被重送一次：不重算、也不改狀態（at-least-once 的常態）。"""
        provider = _CountingProvider()
        document_id = _chunked_document(TENANT_A, chunk_count=2)
        service = _service(provider)
        service.embed_document(TENANT_A, document_id)
        calls = len(provider.calls)

        service.embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            document = Document.objects.get(id=document_id)
        assert document.status == "ready"
        assert len(provider.calls) == calls


class TestTenantIsolation:
    def test_vectors_carry_the_tenant(self, tenants: None) -> None:
        document_id = _chunked_document(TENANT_A, chunk_count=2)

        _service(_CountingProvider()).embed_document(TENANT_A, document_id)

        with tenant_scope(TENANT_A):
            rows = list(Embedding.objects.filter(chunk__document_id=document_id))
        assert rows and all(row.tenant_id == TENANT_A for row in rows)

    def test_another_tenant_cannot_embed_the_document(self, tenants: None) -> None:
        """租戶來自任務參數而非請求——參數錯了是程式錯誤，要在碰到任何資料之前停下來。

        安靜地回「沒有 chunk 要算」是更糟的結果：那會把文件標成 ready，而它一個向量
        都沒有。
        """
        provider = _CountingProvider()
        document_id = _chunked_document(TENANT_A, chunk_count=2)

        with pytest.raises(NotFoundError):
            _service(provider).embed_document(TENANT_B, document_id)

        assert provider.calls == []

"""驗收：Celery 組態與 task 的薄包裝（02 §2、08 §6、CLAUDE.md 鐵則 8）。

**task 是薄包裝**：取 context → 呼叫 service → 回報。邏輯寫進 task 的話，它就只能
被 Celery 呼叫——重跑一份文件得發一個訊息、測試要起 broker，而 service 層明明可以
直接呼叫。

**佇列要分開**（08 §1 的資源隔離）：ETL 吃 CPU 與記憶體，與 embedding、default 擠在
同一個 worker 時，一份 500 頁的 PDF 會讓所有租戶的所有背景工作一起等。

**broker 位址來自設定**（鐵則 9）：hardcode 的話，正式環境會安靜地連到 localhost。
"""

from __future__ import annotations

from config.celery_app import celery_app
from core.tasks import EMBED_DOCUMENT_TASK, INGEST_DOCUMENT_TASK


class TestQueues:
    def test_etl_has_its_own_queue(self) -> None:
        routes = celery_app.conf.task_routes or {}

        assert routes[INGEST_DOCUMENT_TASK]["queue"] == "etl"

    def test_the_default_queue_is_not_etl(self) -> None:
        """預設佇列不得是 etl：沒有指定路由的任務不該落進吃資源的那條。"""
        assert celery_app.conf.task_default_queue != "etl"

    def test_embedding_has_its_own_queue(self) -> None:
        """embedding 與 etl **分開**（06 §2 的 Q2、08 §1）。

        兩者的資源特性相反：ETL 吃 CPU 與記憶體，embedding 吃的是外部 API 的等待
        時間。合在一條佇列時，一份 500 頁 PDF 的解析會擋住所有租戶已經切好塊、只差
        算向量的文件——而那些文件明明不需要 CPU。症狀是「佇列深度正常，但東西就是
        不會變 ready」。
        """
        routes = celery_app.conf.task_routes or {}

        assert routes[EMBED_DOCUMENT_TASK]["queue"] == "embedding"
        assert routes[EMBED_DOCUMENT_TASK]["queue"] != routes[INGEST_DOCUMENT_TASK]["queue"]


class TestBroker:
    def test_broker_is_not_hardcoded(self) -> None:
        """位址來自 Pydantic Settings（與 `core.redis` 同一份 Redis 組態）。"""
        from config.settings.app_settings import get_app_settings

        settings = get_app_settings()

        assert celery_app.conf.broker_url == settings.redis_url.get_secret_value()

    def test_results_are_not_stored(self) -> None:
        """不開 result backend。

        ETL 的進度**已經**在 `etl_jobs` 與 `document.status` 裡，那才是使用者與維運
        看得到的東西。再開一份 Celery 結果儲存，等於同一件事有兩個會漂的來源，而
        且它有 TTL——過期之後「這份文件怎麼了」只剩一個空的查詢結果。
        """
        assert not celery_app.conf.result_backend


class TestReliability:
    def test_tasks_are_acknowledged_after_completion(self) -> None:
        """``acks_late``：worker 被砍時任務要回到佇列，而不是消失。

        預設是「收到就 ack」——worker 在解析途中被 OOM killer 收掉時，那份文件會
        永遠停在 parsing，沒有任何錯誤、也沒有人會重送。冪等（08 §6）正是為了讓這條
        設定安全。
        """
        assert celery_app.conf.task_acks_late is True

    def test_one_task_at_a_time_per_child(self) -> None:
        """``prefetch_multiplier=1``：ETL 任務長短差異極大（1 頁 vs 500 頁）。

        預設會一次抓四個任務，於是短任務排在長任務後面乾等，而佇列看起來是空的。
        """
        assert celery_app.conf.worker_prefetch_multiplier == 1


class TestTaskIsThin:
    def test_the_task_delegates_to_the_service(self, monkeypatch: object) -> None:
        """task 本身不做業務邏輯——它只把參數轉成 service 呼叫。"""
        import uuid

        from worker import etl_tasks

        calls: list[tuple[uuid.UUID, uuid.UUID]] = []

        from services.knowledge.ingestion import IngestionResult

        class _FakeService:
            def ingest(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> IngestionResult:
                calls.append((tenant_id, document_id))
                return IngestionResult(
                    document_id=document_id, status="chunked", chunk_count=3, stats={}
                )

        monkeypatch.setattr(etl_tasks, "IngestionService", _FakeService)  # type: ignore[attr-defined]
        tenant_id, document_id = uuid.uuid4(), uuid.uuid4()

        etl_tasks.ingest_document.run(str(tenant_id), str(document_id))

        assert calls == [(tenant_id, document_id)]

    def test_a_missing_document_is_not_retried(self, monkeypatch: object) -> None:
        """文件不見了就不要重試。

        使用者在排隊期間刪掉文件是**正常操作**——重跑四次的結果都一樣，而那會在 DLQ
        留下一筆看起來像故障的紀錄。刪除不該長得像事故。
        """
        import uuid

        from core.exceptions import NotFoundError
        from worker import etl_tasks

        class _MissingService:
            def ingest(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> object:
                raise NotFoundError("文件不存在")

        monkeypatch.setattr(etl_tasks, "IngestionService", _MissingService)  # type: ignore[attr-defined]

        result = etl_tasks.ingest_document.run(str(uuid.uuid4()), str(uuid.uuid4()))

        assert result["status"] == "missing"

    def test_the_embedding_task_delegates_to_the_service(self, monkeypatch: object) -> None:
        """embedding task 同樣是薄包裝——批次、冪等、失敗分類全在 service。

        寫進 task 的話，重算一份文件就得發一個訊息、測試得起 broker，而 1C-4 的檢索
        評測需要在行程內反覆重算。
        """
        import uuid

        from worker import embedding_tasks

        calls: list[tuple[uuid.UUID, uuid.UUID]] = []

        from services.knowledge.embedding import EmbeddingResult

        class _FakeService:
            def embed_document(
                self, tenant_id: uuid.UUID, document_id: uuid.UUID
            ) -> EmbeddingResult:
                calls.append((tenant_id, document_id))
                return EmbeddingResult(
                    document_id=document_id, status="ready", embedded_count=3, stats={}
                )

        monkeypatch.setattr(embedding_tasks, "EmbeddingService", _FakeService)  # type: ignore[attr-defined]
        tenant_id, document_id = uuid.uuid4(), uuid.uuid4()

        embedding_tasks.embed_document.run(str(tenant_id), str(document_id))

        assert calls == [(tenant_id, document_id)]

    def test_a_missing_document_is_not_retried_by_the_embedding_task(
        self, monkeypatch: object
    ) -> None:
        """理由與 ETL 那條相同：刪除是正常操作，不該在 DLQ 長得像事故。"""
        import uuid

        from core.exceptions import NotFoundError
        from worker import embedding_tasks

        class _MissingService:
            def embed_document(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> object:
                raise NotFoundError("文件不存在")

        monkeypatch.setattr(embedding_tasks, "EmbeddingService", _MissingService)  # type: ignore[attr-defined]

        result = embedding_tasks.embed_document.run(str(uuid.uuid4()), str(uuid.uuid4()))

        assert result["status"] == "missing"

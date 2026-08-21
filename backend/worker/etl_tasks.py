"""ETL 的 Celery task（08 §2、§6）。

**三行原則**（鐵則 8）：把參數轉成型別 → 呼叫 service → 回報。狀態機、冪等、失敗
分類全在 `IngestionService`。

**重試只給可重試的錯誤。** 毒檔與壞檔由 service 記成永久失敗並正常回傳——重試三次
只是把同一個錯誤做三遍，每次都吃掉一個 worker 的 CPU 與記憶體。冒到這裡的例外因此
一律視為基礎設施問題（物件儲存、DB、broker），走 08 §6 的退避重試。
"""

from __future__ import annotations

import uuid
from typing import Any

from celery import shared_task

from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from core.exceptions import NotFoundError
from core.tasks import INGEST_DOCUMENT_TASK, enqueue_ingestion
from services.knowledge.ingestion import IngestionService
from services.platform.fairness import TenantSlotLimiter

logger = get_logger(__name__)

# 08 §6：每 stage 獨立重試 ≤3、指數退避 30s / 2m / 10m。
_RETRY_BACKOFF_SECONDS = (30, 120, 600)
_MAX_RETRIES = len(_RETRY_BACKOFF_SECONDS)


@shared_task(
    name=INGEST_DOCUMENT_TASK,
    bind=True,
    max_retries=_MAX_RETRIES,
    # 任務參數是字串（Celery 只收 JSON，見 config/celery_app.py）。
    acks_late=True,
)
def ingest_document(self: Any, tenant_id: str, document_id: str) -> dict[str, Any]:
    """把一份文件跑完 Extract → Clean → Chunk。"""
    tenant_uuid = uuid.UUID(tenant_id)
    # 公平佇列（08 §6，2A-2b）：這個租戶的並發已滿就讓位——重新排隊（帶延遲）並
    # 立刻讓出 worker，佇列裡下一個（別的租戶）馬上有人服務。**不是 self.retry**：
    # retry 會吃掉 3 次錯誤重試的額度，排隊夠長時任務會被誤判成故障進 DLQ。
    limiter = TenantSlotLimiter("etl")
    if not limiter.acquire(tenant_uuid):
        enqueue_ingestion(
            tenant_id=tenant_uuid,
            document_id=uuid.UUID(document_id),
            delay_seconds=get_app_settings().etl_fairness_requeue_seconds,
        )
        logger.info("ingestion_task_deferred", document_id=document_id, tenant_id=tenant_id)
        return {"document_id": document_id, "status": "deferred", "chunk_count": 0}
    try:
        return _run_ingest(self, tenant_id, document_id)
    finally:
        # 每一種出口（成功、missing、重試、DLQ）都要歸還——漏一條路徑，這個租戶的
        # ETL 併發會慢慢降到零，症狀是「他的文件全卡住，別人都正常」。
        limiter.release(tenant_uuid)


def _run_ingest(self: Any, tenant_id: str, document_id: str) -> dict[str, Any]:
    service = IngestionService()
    try:
        result = service.ingest(uuid.UUID(tenant_id), uuid.UUID(document_id))
    except NotFoundError:
        # 文件不見了（使用者在排隊期間刪掉，或任務參數有誤）。**不重試**：兩種情況
        # 重跑四次的結果都一樣，而那會在 DLQ 留下一筆看起來像故障的紀錄——刪除是
        # 正常操作，不該長得像事故。
        logger.info("ingestion_task_document_missing", document_id=document_id)
        return {"document_id": document_id, "status": "missing", "chunk_count": 0}
    except Exception as exc:
        attempts = self.request.retries + 1
        if attempts > _MAX_RETRIES:
            # **重試耗盡的落點**（08 §6 的 DLQ）。不寫下來的話，這次失敗只存在於
            # worker 的 log 裡：文件會永遠停在 parsing，而使用者與維運都沒有東西可看。
            # 標成 failed 且 ``retryable=True``——與毒檔（False）分得開，因為處置不同：
            # 這種要修環境後重跑，那種重跑幾次都一樣。
            service.mark_retries_exhausted(
                uuid.UUID(tenant_id), uuid.UUID(document_id), exc, attempts=attempts
            )
            raise

        countdown = _RETRY_BACKOFF_SECONDS[min(self.request.retries, _MAX_RETRIES - 1)]
        logger.warning(
            "ingestion_task_retrying",
            document_id=document_id,
            attempt=attempts,
            countdown=countdown,
            exc_info=True,
        )
        raise self.retry(exc=exc, countdown=countdown) from exc

    return {
        "document_id": str(result.document_id),
        "status": result.status,
        "chunk_count": result.chunk_count,
    }

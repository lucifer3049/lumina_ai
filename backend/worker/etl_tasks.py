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
from core.tasks import INGEST_DOCUMENT_TASK
from services.knowledge.ingestion import IngestionService

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
    try:
        result = IngestionService().ingest(uuid.UUID(tenant_id), uuid.UUID(document_id))
    except Exception as exc:
        countdown = _RETRY_BACKOFF_SECONDS[min(self.request.retries, _MAX_RETRIES - 1)]
        logger.warning(
            "ingestion_task_retrying",
            document_id=document_id,
            attempt=self.request.retries + 1,
            countdown=countdown,
            exc_info=True,
        )
        # 重試耗盡時 Celery 讓原例外冒出去 → 任務落 DLQ（08 §6）。文件的狀態由
        # 下一次人工重跑或清理流程處理：這裡不強制標 failed，因為「暫時性錯誤重試
        # 用完」與「這份文件壞了」是不同的事，混成同一個狀態會讓重跑的判斷失去依據。
        raise self.retry(exc=exc, countdown=countdown) from exc

    return {
        "document_id": str(result.document_id),
        "status": result.status,
        "chunk_count": result.chunk_count,
    }

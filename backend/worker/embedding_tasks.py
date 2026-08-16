"""embedding 的 Celery task（06 §2、08 §6）。

形狀與 `worker/etl_tasks.py` 完全相同（鐵則 8 的三行原則）：把參數轉成型別 → 呼叫
service → 回報。批次、冪等、失敗分類全在 `EmbeddingService`。

**冒到這裡的例外一律視為可重試**：永久失敗（配額用盡、模型未啟用）由 service 記進
`document.error` 後正常回傳，不會走到這裡。因此這裡看到的只有基礎設施與 provider
的暫時性問題（429、5xx、逾時、DB 斷線），走 08 §6 的退避重試。
"""

from __future__ import annotations

import uuid
from typing import Any

from celery import shared_task

from config.logging import get_logger
from core.exceptions import NotFoundError
from core.tasks import EMBED_DOCUMENT_TASK
from services.knowledge.embedding import EmbeddingService

logger = get_logger(__name__)

# 08 §6：每 stage 獨立重試 ≤3、指數退避 30s / 2m / 10m。
_RETRY_BACKOFF_SECONDS = (30, 120, 600)
_MAX_RETRIES = len(_RETRY_BACKOFF_SECONDS)


@shared_task(
    name=EMBED_DOCUMENT_TASK,
    bind=True,
    max_retries=_MAX_RETRIES,
    acks_late=True,
)
def embed_document(self: Any, tenant_id: str, document_id: str) -> dict[str, Any]:
    """把一份文件的 chunk 全部轉成向量，成功後文件變成 ``ready``。"""
    service = EmbeddingService()
    try:
        result = service.embed_document(uuid.UUID(tenant_id), uuid.UUID(document_id))
    except NotFoundError:
        # 文件不見了（使用者在排隊期間刪掉，或任務參數有誤）。**不重試**：理由與 ETL
        # 那邊相同——刪除是正常操作，不該在 DLQ 留下一筆看起來像故障的紀錄。
        logger.info("embedding_task_document_missing", document_id=document_id)
        return {"document_id": document_id, "status": "missing", "embedded_count": 0}
    except Exception as exc:
        attempts = self.request.retries + 1
        if attempts > _MAX_RETRIES:
            # 重試耗盡的落點（08 §6 的 DLQ）。不寫下來的話，這次失敗只存在於 worker
            # 的 log 裡：文件會永遠停在 embedding，而使用者與維運都沒有東西可看。
            service.mark_retries_exhausted(
                uuid.UUID(tenant_id), uuid.UUID(document_id), exc, attempts=attempts
            )
            raise

        countdown = _RETRY_BACKOFF_SECONDS[min(self.request.retries, _MAX_RETRIES - 1)]
        logger.warning(
            "embedding_task_retrying",
            document_id=document_id,
            attempt=attempts,
            countdown=countdown,
            exc_info=True,
        )
        raise self.retry(exc=exc, countdown=countdown) from exc

    return {
        "document_id": str(result.document_id),
        "status": result.status,
        "embedded_count": result.embedded_count,
    }

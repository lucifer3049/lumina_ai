"""KB 重建的 Celery task（06 §2.2、2B-6）。

三行原則（鐵則 8）：取 context → 呼叫 service → 回報。四步流程、批次、切換判定全在
`KbReindexService`。

**與 `embedding_tasks` 的差別在收尾**：那一支處理一份文件，跑完就結束；這一支要把
一個 KB 推到終局，而中間有一段是**等別人做完**（重切階段等 ETL 把文件重新處理成
ready）。等待不佔 worker——推不動就重排一次自己，延遲由 `reindex_poll_seconds`
決定。忙等的話，一個上千份文件的 KB 會讓一條 worker 執行緒空轉幾十分鐘。

冪等靠 `advance` 自己：它每一輪都從 DB 讀狀態，重複投遞最多是多查一次。
"""

from __future__ import annotations

import uuid
from typing import Any

from celery import shared_task

from config.logging import get_logger
from core.exceptions import NotFoundError
from core.tasks import REINDEX_KB_TASK, enqueue_reindex
from services.knowledge.reindex import KbReindexService
from services.knowledge.reindex_plan import STATUS_COMPLETED, STATUS_FAILED

logger = get_logger(__name__)

# 推不動時隔多久回來看一次。重切階段等的是整條 ETL（解析 + 切塊 + 向量），以分鐘計
# ——每 5 秒回來一次只是把同一個查詢做 12 倍。
_POLL_SECONDS = 60

# 一次 task 最多推幾輪。**不是安全上限，是公平性**：一個 KB 連續推到底的話，同一條
# worker 執行緒在這段期間不會去看別的租戶的重建。推到上限就重排自己，位置排到隊尾。
_MAX_ROUNDS_PER_TASK = 20


@shared_task(name=REINDEX_KB_TASK, bind=True, acks_late=True)
def reindex_kb(self: Any, tenant_id: str, job_id: str) -> dict[str, Any]:
    """把一個重建 job 推到終局（或推到推不動為止）。"""
    tenant_uuid = uuid.UUID(tenant_id)
    job_uuid = uuid.UUID(job_id)
    service = KbReindexService()

    try:
        view = service.advance(tenant_uuid, job_uuid)
    except NotFoundError:
        # job 或 KB 不見了（KB 被刪、或參數有誤）。**不重試**：刪除是正常操作，
        # 不該在 DLQ 留下一筆看起來像故障的紀錄（同 embedding_tasks 的理由）。
        logger.info("reindex_task_job_missing", job_id=job_id)
        return {"job_id": job_id, "status": "missing"}

    rounds = 1
    while view.status not in {STATUS_COMPLETED, STATUS_FAILED} and rounds < _MAX_ROUNDS_PER_TASK:
        before = (view.status, view.rechunked_documents, view.embedded_chunks)
        view = service.advance(tenant_uuid, job_uuid)
        rounds += 1
        if (view.status, view.rechunked_documents, view.embedded_chunks) == before:
            # 這一輪什麼都沒動——在等 ETL。讓出執行緒，晚一點再回來。
            enqueue_reindex(tenant_id=tenant_uuid, job_id=job_uuid, delay_seconds=_POLL_SECONDS)
            break
    else:
        if view.status not in {STATUS_COMPLETED, STATUS_FAILED}:
            enqueue_reindex(tenant_id=tenant_uuid, job_id=job_uuid)

    logger.info(
        "reindex_task_finished",
        job_id=job_id,
        status=view.status,
        rounds=rounds,
        embedded_chunks=view.embedded_chunks,
    )
    return {
        "job_id": job_id,
        "status": view.status,
        "embedded_chunks": view.embedded_chunks,
        "rechunked_documents": view.rechunked_documents,
    }

"""系統維護的 Celery task（05 §5.2、04 §8.4，2A-1）。

三行原則（鐵則 8）：呼叫 service → 回報。**不取租戶 context**——分區是系統層的
DDL，不屬於任何租戶（superseded 清理等租戶層維護 job 屬 2A-2，進來時各自取 context）。

冪等天然成立（已存在的分區跳過），因此**不設重試**：Beat 每月都會再跑一次，這次
失敗的部分下次自動補上，而 migration 預建的 12 個月讓「連續幾次失敗」離真正的
危險（分區用完）還很遠——integration 的 3 個月守門測試會在那之前紅。
"""

from __future__ import annotations

from typing import Any

from celery import shared_task

from config.logging import get_logger
from core.tasks import (
    ANALYTICS_ROLLUP_TASK,
    CLEANUP_CHUNKS_TASK,
    MAINTAIN_PARTITIONS_TASK,
    RECONCILE_QUOTA_TASK,
    RESCUE_STUCK_DOCUMENTS_TASK,
)
from services.knowledge.cleanup import ChunkCleanupService
from services.knowledge.rescue import StuckDocumentRescueService
from services.platform.analytics import UsageRollupService
from services.platform.maintenance import ensure_future_partitions
from services.platform.reconciliation import QuotaReconciliationService

logger = get_logger(__name__)


@shared_task(name=MAINTAIN_PARTITIONS_TASK)
def maintain_partitions() -> dict[str, Any]:
    """把所有分區表的未來分區補到 3 個月（05 §5.2 的「月初預建下 3 個月」）。"""
    created = ensure_future_partitions(months_ahead=3)
    logger.info("partition_maintenance_done", created=created)
    return {"created": created}


@shared_task(name=RECONCILE_QUOTA_TASK)
def reconcile_quota() -> dict[str, Any]:
    """quota 日結對帳（04 §8.1、2A-2b）：DB 蓋 Redis＋quota_counters 快照。

    逐租戶的失敗處理在 service（單一租戶失敗不中斷整輪）；同樣不設 Celery 重試
    ——明天的日結就是重試。
    """
    processed = QuotaReconciliationService().reconcile_all()
    logger.info("quota_reconciliation_done", tenants=processed)
    return {"tenants": processed}


@shared_task(name=CLEANUP_CHUNKS_TASK)
def cleanup_chunks() -> dict[str, Any]:
    """superseded chunk 的每日清理（06 §2.2、2A-2b）。"""
    purged = ChunkCleanupService().purge_all()
    logger.info("chunk_cleanup_done", purged=purged)
    return {"purged": purged}


@shared_task(name=RESCUE_STUCK_DOCUMENTS_TASK)
def rescue_stuck_documents() -> dict[str, Any]:
    """把停滯的文件補送回佇列（enqueue 是 best-effort，訊息會丟；2A-2b 收尾）。"""
    rescued = StuckDocumentRescueService().rescue_all()
    if rescued:
        logger.info("stuck_rescue_done", rescued=rescued)
    return {"rescued": rescued}


@shared_task(name=ANALYTICS_ROLLUP_TASK)
def rollup_usage() -> dict[str, Any]:
    """usage 的每小時彙總（usage_logs → usage_daily，2A-3）。"""
    processed = UsageRollupService().rollup_all()
    logger.info("usage_rollup_done", tenants=processed)
    return {"tenants": processed}

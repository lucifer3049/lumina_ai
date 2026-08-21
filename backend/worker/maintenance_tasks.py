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
from core.tasks import MAINTAIN_PARTITIONS_TASK
from services.platform.maintenance import ensure_future_partitions

logger = get_logger(__name__)


@shared_task(name=MAINTAIN_PARTITIONS_TASK)
def maintain_partitions() -> dict[str, Any]:
    """把所有分區表的未來分區補到 3 個月（05 §5.2 的「月初預建下 3 個月」）。"""
    created = ensure_future_partitions(months_ahead=3)
    logger.info("partition_maintenance_done", created=created)
    return {"created": created}

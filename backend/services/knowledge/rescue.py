"""停滯文件的補償掃描（08 §2 狀態機的縫隙，2A-2b 收尾）。

`enqueue_*` 是 best-effort（broker 掛掉不讓上傳失敗，1B-3），代價是訊息可能
遺失：文件停在 `uploaded`（沒人去 ingest）或 `chunked`（沒人去 embed），
看起來只是「還在處理」。這裡定期把停超過門檻的補送回對應佇列。

**只管這兩個狀態。** parsing／embedding 的斷裂由 acks_late 與重試管（worker
死掉訊息回佇列）；ready／failed 是終局。門檻（`etl_stuck_after_seconds`）擋
「正常也會短暫停留」的誤判——冪等擋得住重算，擋不住每份文件都送兩次的浪費。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from core.tasks import enqueue_embedding, enqueue_ingestion
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import TenantDirectoryRepository
from repositories.knowledge import DocumentRepository

logger = get_logger(__name__)

__all__ = ["StuckDocumentRescueService"]

# 只救這兩個狀態（模組 docstring）。狀態與任務的對應在 `_enqueue_for`——
# 對錯邊的話，service 的狀態防呆會安靜返回，文件繼續停著，而掃描器每輪都「成功」。
_RESCUE_STATUSES = ("uploaded", "chunked")


def _enqueue_for(status: str) -> Callable[..., str | None]:
    # 呼叫時才查模組全域（不用 import 期抓參照的 dict）：測試要能以 monkeypatch
    # 攔截 enqueue_*，而 dict 裡的舊參照攔不到。
    return enqueue_ingestion if status == "uploaded" else enqueue_embedding


class StuckDocumentRescueService:
    def __init__(
        self,
        *,
        documents: DocumentRepository | None = None,
        directory: TenantDirectoryRepository | None = None,
    ) -> None:
        self._documents = documents or DocumentRepository()
        self._directory = directory or TenantDirectoryRepository()

    def rescue_tenant(self, tenant_id: uuid.UUID) -> int:
        threshold = datetime.now(UTC) - timedelta(
            seconds=get_app_settings().etl_stuck_after_seconds
        )
        # `stuck_in` 是 1C-3 就有的維運查詢（含「為什麼必須帶時間下限」的說明）
        # ——本掃描器就是把當年的手動恢復指令自動化，共用同一份語意。
        with tenant_context(tenant_id), unit_of_work():
            stuck = [
                (uuid.UUID(str(document.id)), str(document.status))
                for document in self._documents.stuck_in(
                    list(_RESCUE_STATUSES), not_updated_since=threshold
                )
            ]
        rescued = 0
        for document_id, status in stuck:
            # 送在交易之外（同上傳路徑的規矩：交易內送出的話 worker 可能在
            # COMMIT 前開跑）。送失敗不中斷——下一輪掃描就是重試。
            _enqueue_for(status)(tenant_id=tenant_id, document_id=document_id)
            rescued += 1
            logger.info(
                "stuck_document_rescued",
                tenant_id=str(tenant_id),
                document_id=str(document_id),
                status=status,
            )
        return rescued

    def rescue_all(self) -> int:
        """逐 active 租戶掃描（Beat 每 15 分鐘）；回傳補送的文件數。"""
        total = 0
        for tenant_id in self._directory.active_tenant_ids():
            try:
                total += self.rescue_tenant(tenant_id)
            except Exception:
                logger.exception("stuck_rescue_failed", tenant_id=str(tenant_id))
        return total

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
from core.tasks import enqueue_embedding, enqueue_ingestion, enqueue_reindex
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import TenantDirectoryRepository
from repositories.knowledge import DocumentRepository, KbReindexJobRepository

logger = get_logger(__name__)

__all__ = ["StuckDocumentRescueService", "StuckReindexRescueService"]

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


class StuckReindexRescueService:
    """停滯的 KB 重建 job（2B-6 缺口③）。

    `enqueue_reindex` 與上面那支一樣是 best-effort，代價也一樣：job 停在 `pending`
    而沒有任何訊息存在。**但後果嚴重一級**——`pending` 也算「進行中」，而「同一個 KB
    只能有一個進行中的 job」是 DB 約束，所以停住的那一個會把這個 KB 的重建**永久卡
    死**（使用者再按只會拿到 409）。

    兩種停滯的處置刻意不同：

    - **`pending`（還沒開始）→ 補送。** 這一版還沒有任何向量被算出來，重送最多是多查
      一次。
    - **做到一半 → 標 `failed`，不補送。** 補送等於讓兩個 ``advance`` 併行，而它們會
      各自列出「還缺向量的 chunk」然後各算一次——同一批付兩次錢。標成 failed 讓使用者
      重新發起，而已算好的向量留著（新 job 會跳過它們），一毛都不浪費。
      這與 `StuckStreamRescueService`「就地標成中斷」是同一種處置：**沒有便宜的重送
      方式時，就把狀態誠實地寫成終局，把決定權交回使用者。**
    """

    _REQUEUE_STATUSES = ("pending",)
    _FAIL_STATUSES = ("rechunking", "embedding")

    def __init__(
        self,
        *,
        jobs: KbReindexJobRepository | None = None,
        directory: TenantDirectoryRepository | None = None,
    ) -> None:
        self._jobs = jobs or KbReindexJobRepository()
        self._directory = directory or TenantDirectoryRepository()

    def rescue_tenant(self, tenant_id: uuid.UUID) -> int:
        """處理一個租戶；回傳「補送 ＋ 標成失敗」的 job 數。"""
        threshold = datetime.now(UTC) - timedelta(
            seconds=get_app_settings().reindex_stuck_after_seconds
        )
        with tenant_context(tenant_id), unit_of_work():
            pending = [
                uuid.UUID(str(job.id))
                for job in self._jobs.stuck_in(
                    list(self._REQUEUE_STATUSES), not_updated_since=threshold
                )
            ]
            stalled = [
                (uuid.UUID(str(job.id)), str(job.status))
                for job in self._jobs.stuck_in(
                    list(self._FAIL_STATUSES), not_updated_since=threshold
                )
            ]
            for job_id, status in stalled:
                self._jobs.update(
                    job_id,
                    status="failed",
                    error={
                        "cause": "ReindexStalled",
                        # 訊息會出現在使用者眼前，所以要說得出「接下來怎麼辦」。
                        "message": "重建停滯逾時，已中止；可重新發起（已算好的向量會沿用）",
                    },
                    finished_at=datetime.now(UTC),
                )
                logger.warning(
                    "stuck_reindex_job_failed",
                    tenant_id=str(tenant_id),
                    job_id=str(job_id),
                    status=status,
                )

        for job_id in pending:
            # 送在交易之外（同上傳路徑的規矩）。送失敗不中斷——下一輪就是重試。
            enqueue_reindex(tenant_id=tenant_id, job_id=job_id)
            logger.info("stuck_reindex_job_requeued", tenant_id=str(tenant_id), job_id=str(job_id))

        return len(pending) + len(stalled)

    def rescue_all(self) -> int:
        """逐 active 租戶掃描（與文件的補償掃描同一個 Beat 任務）。"""
        total = 0
        for tenant_id in self._directory.active_tenant_ids():
            try:
                total += self.rescue_tenant(tenant_id)
            except Exception:
                logger.exception("stuck_reindex_rescue_failed", tenant_id=str(tenant_id))
        return total

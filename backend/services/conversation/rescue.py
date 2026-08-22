"""卡在 `streaming` 的訊息的補償掃描（05 §3.4 狀態機的縫隙）。

`ChatService` 只收得了尾**優雅的那一種**：關機時 `drain()` 送出 `CancelledError`，
那條路徑會 shield 住收尾，把訊息標成 `interrupted` 並保住已產生的部分。

**硬殺沒有那條路徑。** OOM、`kill -9`、機器沒了——行程消失的那一刻，那一列還是
`streaming`，而且**再也沒有人會去動它**：生成不是 Celery task（沒有 acks_late 把它
還回佇列），也沒有任何一支排程掃描它。使用者看到的是永遠停在「正在輸入」的一則
訊息，重新整理也一樣，因為那就是資料庫裡的狀態。

文件那邊早就有這件事（`services/knowledge/rescue.py`），訊息這邊沒有。這個模組是
同一個形狀：逐 active 租戶、掃停太久的列、就地收尾。

**門檻好定**：生成本身有 120 秒的牆鐘上限（06 §4），所以停超過
`stream_stuck_after_seconds`（預設 10 分鐘）只有一個解釋——產生它的行程已經不在了。
太短的話會把還在跑的長回答標成中斷，而那一輪之後還會寫回 completed，於是使用者
看到訊息「先中斷再完成」。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from core.exceptions import ErrorCode
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.conversation import MessageRepository
from repositories.identity import TenantDirectoryRepository

logger = get_logger(__name__)

__all__ = ["StuckStreamRescueService"]

_STATUS_INTERRUPTED = "interrupted"

# 收尾時寫進 `message.error` 的原因。與 `ChatService._fail` 的關機路徑同一個 code
# （09 附錄 A 的 `STREAM_INTERRUPTED`）：對使用者是同一件事——「這段沒有寫完」，
# 而 `cause` 分得出是誰收的尾，那是排查時唯一有用的差別。
_CAUSE = "worker_lost"


class StuckStreamRescueService:
    def __init__(
        self,
        *,
        messages: MessageRepository | None = None,
        directory: TenantDirectoryRepository | None = None,
    ) -> None:
        self._messages = messages or MessageRepository()
        self._directory = directory or TenantDirectoryRepository()

    def rescue_tenant(self, tenant_id: uuid.UUID) -> int:
        """把這個租戶停太久的 `streaming` 訊息標成 `interrupted`；回傳筆數。

        **不清空 content**：已經產生的那半句話要留著（09 附錄 A 對
        `STREAM_INTERRUPTED` 的要求就是「partial 已保存」），使用者看得到生成到哪裡
        比看到一則空訊息有用得多。
        """
        threshold = datetime.now(UTC) - timedelta(
            seconds=get_app_settings().stream_stuck_after_seconds
        )
        with tenant_context(tenant_id), unit_of_work():
            stuck = [
                uuid.UUID(str(message.id))
                for message in self._messages.stuck_streaming(started_before=threshold)
            ]
            for message_id in stuck:
                self._messages.set_status(
                    message_id,
                    status=_STATUS_INTERRUPTED,
                    error={"code": str(ErrorCode.STREAM_INTERRUPTED), "cause": _CAUSE},
                )

        for message_id in stuck:
            logger.warning(
                "stuck_stream_rescued", tenant_id=str(tenant_id), message_id=str(message_id)
            )
        return len(stuck)

    def rescue_all(self) -> int:
        """逐 active 租戶掃描（Beat）；回傳收尾的訊息數。

        單一租戶失敗不中斷整輪（同文件那支）：一個壞掉的租戶不該讓其他所有租戶的
        訊息繼續掛在「正在輸入」。
        """
        total = 0
        for tenant_id in self._directory.active_tenant_ids():
            try:
                total += self.rescue_tenant(tenant_id)
            except Exception:
                logger.exception("stuck_stream_rescue_failed", tenant_id=str(tenant_id))
        return total

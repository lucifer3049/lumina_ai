"""通知的 email 派送（04 §8.5，2A-5）。

Task 三行原則（鐵則 8）：取 context → 呼叫 service → 回報。

**重試上限刻意小**：SMTP 連不上通常不是幾秒內會好的事，而通知已經寫在收件匣裡
——站內看得到才是最低保證，email 只是「順便送到他眼前」。試到天荒地老只會把
maintenance 佇列塞住。
"""

from __future__ import annotations

import uuid
from typing import Any

from celery import shared_task

from config.logging import get_logger
from core.tasks import SEND_NOTIFICATION_EMAIL_TASK
from services.platform.notifications import NotificationService

logger = get_logger(__name__)

_MAX_RETRIES = 2
_RETRY_COUNTDOWN_SECONDS = 60


@shared_task(name=SEND_NOTIFICATION_EMAIL_TASK, bind=True, max_retries=_MAX_RETRIES)
def send_notification_email(self: Any, tenant_id: str, notification_id: str) -> dict[str, Any]:
    """把一則通知寄成 email。"""
    try:
        sent = NotificationService().send_email(uuid.UUID(tenant_id), uuid.UUID(notification_id))
    except Exception as exc:
        if self.request.retries >= _MAX_RETRIES:
            # 重試耗盡：通知本身還在收件匣裡，所以這裡只留下 log（沒有 DLQ 列）。
            logger.error(
                "notification_email_giving_up",
                notification_id=notification_id,
                exc_info=True,
            )
            return {"notification_id": notification_id, "sent": False}
        raise self.retry(exc=exc, countdown=_RETRY_COUNTDOWN_SECONDS) from exc
    return {"notification_id": notification_id, "sent": sent}

"""UsageService——usage_logs 的唯一寫入口（04 §8.2、05 §3.3，2A-1）。

Gateway 保證 `usage` 事件恰一筆（1C-5），這裡負責讓它落地。Quota 對帳（2A-2）與
Analytics（2A-3）都從 usage_logs 讀——寫入口只有一個，兩邊才對得上帳。

**旁路原則：`record` 不往外拋任何例外。** usage 記錄失敗只能失去這一筆統計，
不能讓使用者的回答消失、或讓一份文件的 embedding 白算。錯誤記 log（含足以人工
補帳的欄位），主流程繼續。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from config.logging import get_logger
from core.tenant import tenant_context
from repositories.platform import UsageLogRepository
from services.platform.pricing import compute_cost

logger = get_logger(__name__)

__all__ = ["UsageEvent", "UsageLogSink", "UsageService"]


class UsageLogSink(Protocol):
    """`record` 需要的最小介面（同 `VectorHitLike` 的慣例：Protocol 宣告在
    consumer 端）——unit 測試的假 repository 靠它通過 mypy。"""

    def add(
        self,
        *,
        category: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: Decimal | None,
        request_id: str,
        user_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """一次消費。`request_id` 對映得回觸發它的呼叫：chat 是 message_id、
    embedding 是文件與版本——對帳查「這一筆是哪一次」全靠它。"""

    category: str
    model: str
    prompt_tokens: int
    request_id: str
    completion_tokens: int = 0
    user_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None


class UsageService:
    def __init__(self, *, usage_logs: UsageLogSink | None = None) -> None:
        self._usage_logs: UsageLogSink = usage_logs or UsageLogRepository()

    def record(self, tenant_id: uuid.UUID, event: UsageEvent) -> None:
        """落一列帳。計價在寫入時發生——之後改價目表不改歷史成本，帳是當時的價。"""
        try:
            cost = compute_cost(
                event.model,
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
            )
            # 交易在 repository 的 add 裡自帶（旁路寫入不搭主流程的交易，理由見
            # `UsageLogRepository.add`）；這裡只負責租戶 context 與吞錯。
            with tenant_context(tenant_id):
                self._usage_logs.add(
                    category=event.category,
                    model=event.model,
                    prompt_tokens=event.prompt_tokens,
                    completion_tokens=event.completion_tokens,
                    cost=cost,
                    request_id=event.request_id,
                    user_id=event.user_id,
                    conversation_id=event.conversation_id,
                )
        except Exception:
            # 記到足以人工補帳的程度：tenant、類別、模型、tokens、對映鍵全在。
            logger.exception(
                "usage_record_failed",
                tenant_id=str(tenant_id),
                category=event.category,
                model=event.model,
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
                request_id=event.request_id,
            )

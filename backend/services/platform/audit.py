"""AuditService——稽核紀錄的唯一寫入口與查詢口（04 §8.3、10 §3，2A-4）。

寫入有兩條路，**刻意的**（04 §8.3：「事件驅動 + middleware 自動記錄」）：

1. `api/middleware/audit.py`：寫入型請求一律經它，欄位來自請求本身。
2. Service 自己呼叫：**請求層記不了的那些**。目前只有登入——那條路徑上沒有
   principal（還沒有人通過認證），也沒有租戶 contextvar（AuthService 自己進出
   `tenant_context`），middleware 拿不到 tenant_id 就寫不出列。

**旁路原則：`record` 不往外拋任何例外。** 與 `UsageService` 同一條理由，但結論更
尖銳：稽核記不成只能失去這一列，而「稽核寫不進去就讓請求失敗」的設計會在稽核表
出問題的那天讓整個平台停擺——那正是最不該再加一個故障點的時刻。失敗記 log（含
足以人工補的欄位）。

查詢是唯讀的（04 §8.3 的 Interface 就只有 `query`）：沒有更新、沒有刪除。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from common.cursors import CursorError, decode_cursor, encode_cursor
from config.logging import get_logger
from core.exceptions import ValidationFailedError
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.platform import AuditLogRepository

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "OUTCOME_DENIED",
    "OUTCOME_FAILED",
    "OUTCOME_SUCCEEDED",
    "AuditEvent",
    "AuditPage",
    "AuditRecord",
    "AuditService",
]

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# 三種結局。`denied` 與 `failed` 分開是因為它們是**不同的調查**：前者是權限問題
# （有人在試他不該做的事，或某個角色設錯了），後者是操作問題（id 不存在、狀態不對）。
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_DENIED = "denied"
OUTCOME_FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """一次敏感操作。欄位對映 05 §3.3 的 audit_logs。"""

    action: str
    resource_type: str
    outcome: str
    request_id: str
    actor_id: uuid.UUID | None = None
    actor_type: str = "user"
    resource_id: uuid.UUID | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    status: int | None = None
    permission: str | None = None
    ip: str | None = None
    user_agent: str = ""


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """查詢結果的一列。**不回傳 ORM 物件**：api 層不認識 `apps`（鐵則 2）。"""

    id: uuid.UUID
    action: str
    actor_id: uuid.UUID | None
    actor_type: str
    resource_type: str
    resource_id: uuid.UUID | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    outcome: str
    status: int | None
    permission: str | None
    ip: str | None
    user_agent: str
    request_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AuditPage:
    items: list[AuditRecord]
    next_cursor: str | None


class AuditService:
    def __init__(self, *, logs: AuditLogRepository | None = None) -> None:
        self._logs = logs or AuditLogRepository()

    def record(self, tenant_id: uuid.UUID, event: AuditEvent) -> None:
        """落地一列稽核。**永遠不拋例外**（旁路原則）。"""
        try:
            with tenant_context(tenant_id):
                self._logs.add(
                    action=event.action,
                    resource_type=event.resource_type,
                    outcome=event.outcome,
                    request_id=event.request_id,
                    actor_id=event.actor_id,
                    actor_type=event.actor_type,
                    resource_id=event.resource_id,
                    before=event.before,
                    after=event.after,
                    status=event.status,
                    permission=event.permission,
                    ip=event.ip,
                    user_agent=event.user_agent,
                )
        except Exception:
            # 欄位寫進 log 是為了人工補帳：這一列的內容在這裡是唯一還存在的地方。
            logger.exception(
                "audit_record_failed",
                tenant_id=str(tenant_id),
                action=event.action,
                outcome=event.outcome,
                actor_id=str(event.actor_id) if event.actor_id else None,
                resource_id=str(event.resource_id) if event.resource_id else None,
                request_id=event.request_id,
            )

    def query(
        self,
        tenant_id: uuid.UUID,
        *,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        start: date | None = None,
        end: date | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> AuditPage:
        """新到舊的一頁。`start`／`end` 是**日期**（含當日），在這裡展開成時間界線
        ——「查 8/22 這一天」寫成 `created_at < 8/23 00:00`，端點的使用者不必自己
        算那個開區間。"""
        try:
            # `unit_of_work` 不只是交易：RLS 的 `app.tenant_id` 是交易區域參數，
            # 少了它 policy 讀到 NULL，查詢一列都不會回（fail closed，且沒有錯誤）。
            with tenant_context(tenant_id), unit_of_work():
                rows, next_key = self._logs.page(
                    limit=limit,
                    cursor=_decode(cursor),
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    actor_id=actor_id,
                    start=_day_start(start),
                    end=_day_start(end + timedelta(days=1)) if end is not None else None,
                )
        except CursorError as exc:
            # 解得開但內容不對（少了鍵、時間格式壞了）——同樣是 client 的錯，
            # 而它是在 repository 裡才發現的，兩個地方都要轉。
            raise ValidationFailedError("游標不正確") from exc
        return AuditPage(
            items=[
                AuditRecord(
                    id=uuid.UUID(str(row.id)),
                    action=row.action,
                    actor_id=row.actor_id,
                    actor_type=row.actor_type,
                    resource_type=row.resource_type,
                    resource_id=row.resource_id,
                    before=row.before,
                    after=row.after,
                    outcome=row.outcome,
                    status=row.status,
                    permission=row.permission,
                    ip=str(row.ip) if row.ip else None,
                    user_agent=row.user_agent,
                    request_id=row.request_id,
                    created_at=row.created_at,
                )
                for row in rows
            ],
            next_cursor=encode_cursor(next_key) if next_key else None,
        )


def _day_start(day: date | None) -> datetime | None:
    """日界一律以 UTC 算——與 `created_at` 的儲存時區一致。租戶時區的日界屬
    Dashboard 的顯示問題，不在這一層（同 2A-3 的 analytics）。"""
    return datetime.combine(day, time.min, tzinfo=UTC) if day is not None else None


def _decode(cursor: str | None) -> dict[str, Any] | None:
    """游標字串 → dict；壞掉是 422 而不是「當成第一頁」（理由見 common/cursors.py）。"""
    if cursor is None:
        return None
    try:
        return decode_cursor(cursor)
    except CursorError as exc:
        raise ValidationFailedError("游標不正確") from exc

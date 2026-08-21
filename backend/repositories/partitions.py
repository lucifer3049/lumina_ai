"""分區維護——每月把分區表的未來分區補齊（05 §5.2，2A-1）。

1D-1 建 `conversation_message` 時預建了 12 個月並留話「Beat 屬 2A」；這裡就是那個
Beat 的實作本體。放 `repositories/` 而不是 `services/`：它是純 DB 操作（DDL），且
RLS 的 SQL 要與各 app migration 用**同一個產生器**——services 層禁 import `apps`
（import-linter），repository 層可以。

兩個關鍵性質（tests/integration/test_partition_maintenance.py）：

1. **冪等**：已存在的分區跳過，回傳值只含真的新建的。
2. **新分區必帶 RLS**：新分區不會自動繼承 policy，這裡漏了會出現「查父表安全、
   查子分區不安全」的缺口，而它完全無症狀——防線必須建在建分區的這隻手上。

`audit_logs`（2A-4）落地時加進 `PARTITIONED_TABLES`。
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.db import connections

from apps.conversation.migrations import _rls as _conversation_rls
from apps.platform.migrations import _rls as _platform_rls
from config.logging import get_logger

logger = get_logger(__name__)

__all__ = ["PARTITIONED_TABLES", "ensure_future_partitions"]

# 表 → 該 app 的 RLS 產生器。**用各 app 自己的那一份**（內容相同，各 app 一份是
# 既有慣例）：分區的 policy 必須與父表出自同一個來源，兩份 SQL 漂掉的症狀是
# 「某幾個月份沒有隔離」。
PARTITIONED_TABLES = {
    "conversation_message": _conversation_rls.enable,
    "platform_usagelog": _platform_rls.enable,
}


def _month_bounds(table: str, index: int) -> tuple[str, str, str]:
    """第 `index` 個月的分區名與上下界（0 = 當月）。算法同各 migration 的同名函式。"""
    now = datetime.now(UTC)
    year, month = now.year, now.month + index
    year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
    next_year, next_month = (year, month + 1) if month < 12 else (year + 1, 1)
    return (
        f"{table}_{year:04d}_{month:02d}",
        f"{year:04d}-{month:02d}-01",
        f"{next_year:04d}-{next_month:02d}-01",
    )


def _existing_partitions(table: str) -> set[str]:
    with connections["admin"].cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_inherits i ON i.inhrelid = c.oid
            WHERE i.inhparent = %s::regclass
            """,
            [table],
        )
        return {row[0] for row in cursor.fetchall()}


def ensure_future_partitions(months_ahead: int = 3) -> list[str]:
    """把每張註冊表的分區補到未來 `months_ahead` 個月（含當月），回傳新建的分區名。

    系統層操作（DDL），**不進租戶 context**——分區不屬於任何租戶。走 `admin`
    連線（config/settings/base.py：owner 角色的三種用途之一「維運腳本」）：
    應用角色沒有 DDL 權限，而那正是對的——不該有任何一條請求路徑能建表。
    """
    created: list[str] = []
    for table, enable_rls in PARTITIONED_TABLES.items():
        existing = _existing_partitions(table)
        with connections["admin"].cursor() as cursor:
            for index in range(months_ahead + 1):
                name, start, end = _month_bounds(table, index)
                if name in existing:
                    continue
                cursor.execute(
                    f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {table} "
                    f"FOR VALUES FROM ('{start}') TO ('{end}');"
                )
                cursor.execute(enable_rls(name))
                created.append(name)
                logger.info("partition_created", table=table, partition=name)
    return created

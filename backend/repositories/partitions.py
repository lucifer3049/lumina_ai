"""分區維護——每月把分區表的未來分區補齊（05 §5.2，2A-1）。

1D-1 建 `conversation_message` 時預建了 12 個月並留話「Beat 屬 2A」；這裡就是那個
Beat 的實作本體。放 `repositories/` 而不是 `services/`：它是純 DB 操作（DDL），且
RLS 的 SQL 要與各 app migration 用**同一個產生器**——services 層禁 import `apps`
（import-linter），repository 層可以。

兩個關鍵性質（tests/integration/test_partition_maintenance.py）：

1. **冪等**：已存在的分區跳過，回傳值只含真的新建的。
2. **新分區必帶 RLS**：新分區不會自動繼承 policy，這裡漏了會出現「查父表安全、
   查子分區不安全」的缺口，而它完全無症狀——防線必須建在建分區的這隻手上。

三張表到齊（`conversation_message` 1D-1、`platform_usagelog` 2A-1、
`platform_auditlog` 2A-4）；新的分區表一律加進 `PARTITIONED_TABLES`。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime

from django.db import connections, transaction

from apps.conversation.migrations import _rls as _conversation_rls
from apps.platform.migrations import _rls as _platform_rls
from config.logging import get_logger

logger = get_logger(__name__)

__all__ = ["PARTITIONED_TABLES", "detach_expired_partitions", "ensure_future_partitions"]

# 表 → 該 app 的 RLS 產生器。**用各 app 自己的那一份**（內容相同，各 app 一份是
# 既有慣例）：分區的 policy 必須與父表出自同一個來源，兩份 SQL 漂掉的症狀是
# 「某幾個月份沒有隔離」。
PARTITIONED_TABLES = {
    "conversation_message": _conversation_rls.enable,
    "platform_usagelog": _platform_rls.enable,
    "platform_auditlog": _platform_rls.enable,
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


# `pg_get_expr(relpartbound)` 的輸出形如
# `FOR VALUES FROM ('2026-08-01 00:00:00+00') TO ('2026-09-01 00:00:00+00')`。
# **讀真正的上界而不是解析分區名**：名字是我們自己取的慣例，而過期判斷一旦
# 錯了是不可逆的——要問就問資料庫本身。
_UPPER_BOUND = re.compile(r"TO \('([^']+)'\)")

# DETACH 要拿父表的 ACCESS EXCLUSIVE lock。等不到就放棄這一個，下個月再來——
# 維運任務不該把自己排在寫入路徑前面（11 §4.1 的「所有外部呼叫都有 timeout」
# 在 DDL 上的對應）。
_LOCK_TIMEOUT = "5s"


def _partition_bounds(table: str) -> list[tuple[str, datetime]]:
    """(分區名, 上界)；抓不到上界的（DEFAULT 分區）不回傳——不判斷就是不動。"""
    with connections["admin"].cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname, pg_get_expr(c.relpartbound, c.oid)
            FROM pg_class c
            JOIN pg_inherits i ON i.inhrelid = c.oid
            WHERE i.inhparent = %s::regclass
            """,
            [table],
        )
        rows = list(cursor.fetchall())

    bounds: list[tuple[str, datetime]] = []
    for name, expression in rows:
        match = _UPPER_BOUND.search(str(expression))
        if match is None:
            logger.warning("partition_bound_unparsed", table=table, partition=name)
            continue
        bounds.append((str(name), datetime.fromisoformat(match.group(1))))
    return bounds


def detach_expired_partitions(cutoffs: Mapping[str, date], *, drop: bool = False) -> list[str]:
    """把上界 **早於或等於** 界線的分區從父表摘下來；回傳摘掉的分區名。

    `cutoffs` 沒列到的表一律不動（`conversation_message` 的保留期是「依租戶方案」，
    那個機制還不存在——沒有政策時「不動」是唯一安全的預設）。

    **預設只 DETACH**：摘下來的分區變成一張普通表，查詢不再掃到它，空間仍在，
    錯了可以 ATTACH 回去。`drop=True` 才真的刪除，而那是整套維運任務裡唯一
    不可逆的一步（05 §5.2 寫的是 DETACH + DROP，預設只做前半是 2A-4 的決定）。

    單一分區失敗（拿不到 lock、被別的連線占用）不中斷整輪：下個月的 Beat 就是重試。
    """
    detached: list[str] = []
    for table, cutoff in cutoffs.items():
        limit = datetime.combine(cutoff, datetime.min.time(), tzinfo=UTC)
        for name, upper in _partition_bounds(table):
            if upper > limit:
                continue
            try:
                with transaction.atomic(using="admin"), connections["admin"].cursor() as cursor:
                    # SET LOCAL：只在這個交易內生效，不會留給連線上的下一個人
                    # （repositories/base.py 禁的是 session 級的 SET）。
                    cursor.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}';")
                    cursor.execute(f"ALTER TABLE {table} DETACH PARTITION {name};")
                    if drop:
                        cursor.execute(f"DROP TABLE {name};")
            except Exception:
                logger.exception("partition_detach_failed", table=table, partition=name)
                continue
            detached.append(name)
            logger.info("partition_detached", table=table, partition=name, dropped=drop)
    return detached

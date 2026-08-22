"""分區維護的 service 門面（05 §5.2／§7，2A-1／2A-4）。

實作本體在 `repositories/partitions.py`——它是純 DDL，且 RLS 的 SQL 產生器住在各
app 的 migrations 裡，services 層禁 import `apps`（import-linter），repository 層
可以。這裡是 worker 與其他 service 的正式入口（worker task 三行原則：取 context →
呼叫 service → 回報）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from repositories.partitions import (
    PARTITIONED_TABLES,
    detach_expired_partitions,
    ensure_future_partitions,
)

logger = get_logger(__name__)

__all__ = [
    "PARTITIONED_TABLES",
    "cutoff_for",
    "detach_expired_partitions",
    "ensure_future_partitions",
    "prune_expired_partitions",
    "retention_months",
]


def retention_months() -> dict[str, int]:
    """設定字串 → `表名 → 月數`。壞掉的條目跳過（只失去那一張表的清理），
    形狀同 `pricing`／`quota` 的解析。

    **沒列到的表不動**：保留政策要嘛有、要嘛沒有；沒有的時候拿一個猜的預設值
    去摘別人的分區，是拿真資料賭我猜對了。
    """
    months: dict[str, int] = {}
    for entry in get_app_settings().partition_retention_months.split(";"):
        table, _, raw = entry.strip().partition(":")
        if not table:
            continue
        try:
            value = int(raw)
        except ValueError:
            logger.warning("partition_retention_entry_ignored", entry=entry)
            continue
        if value > 0:
            months[table] = value
    return months


def cutoff_for(months: int, *, now: datetime | None = None) -> date:
    """保留 `months` 個月的界線日期：**當月 1 日**往前推 `months` 個月。

    對齊月初而不是「今天往前推」：後者會讓界線在一個月內每天移動，同一個分區
    某天算過期、某天不算——而摘除是不可逆的（預設 DETACH 才留了後路）。
    """
    current = now or datetime.now(UTC)
    total = current.year * 12 + (current.month - 1) - months
    return date(total // 12, total % 12 + 1, 1)


def prune_expired_partitions() -> list[str]:
    """依設定把過期分區從父表摘下來（預設不刪）；回傳摘掉的分區名。"""
    settings = get_app_settings()
    cutoffs = {table: cutoff_for(months) for table, months in retention_months().items()}
    return detach_expired_partitions(cutoffs, drop=settings.partition_drop_after_detach)

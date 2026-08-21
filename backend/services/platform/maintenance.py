"""分區維護的 service 門面（05 §5.2，2A-1）。

實作本體在 `repositories/partitions.py`——它是純 DDL，且 RLS 的 SQL 產生器住在各
app 的 migrations 裡，services 層禁 import `apps`（import-linter），repository 層
可以。這裡是 worker 與其他 service 的正式入口（worker task 三行原則：取 context →
呼叫 service → 回報）。
"""

from __future__ import annotations

from repositories.partitions import PARTITIONED_TABLES, ensure_future_partitions

__all__ = ["PARTITIONED_TABLES", "ensure_future_partitions"]

"""驗收：分區維護——真的建得出分區（05 §5.2，13 §4 工作包 2A-1）。

排程的**註冊**在 tests/unit/test_partition_beat.py；這裡驗函式本身：
`services.platform.maintenance.ensure_future_partitions()`。

兩個關鍵性質：

1. **冪等**。Beat 每月跑、部署時也可能手動跑，重複執行不能是錯誤（
   `CREATE TABLE IF NOT EXISTS` 的語意要一路保持到函式的回傳值）。
2. **新分區必須帶 RLS**。這是 1D-1 就埋好的地雷引線：新分區不會自動繼承
   policy，Beat 建出裸分區的話，會出現「查父表安全、查子分區不安全」的缺口，
   而 test_rls_*.py 的逐分區檢查只掃「現有」分區——防線必須建在建分區的那隻手上。
"""

from __future__ import annotations

import pytest
from django.db import connection

from services.platform.maintenance import PARTITIONED_TABLES, ensure_future_partitions

# admin：分區維護走 owner 連線（應用角色沒有 DDL 權限，見 repositories/partitions.py）。
pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

# 遠到 migration 的 12 個月預建絕對蓋不到，函式才有東西可建。
_FAR_AHEAD = 15


def _partitions_of(table: str) -> dict[str, tuple[bool, bool]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c
            JOIN pg_inherits i ON i.inhrelid = c.oid
            WHERE i.inhparent = %s::regclass
            """,
            [table],
        )
        return {name: (enabled, forced) for name, enabled, forced in cursor.fetchall()}


class TestRegistry:
    def test_both_partitioned_tables_are_registered(self) -> None:
        """名單漏了哪張，哪張的分區就會在 12 個月後用完——而漏掉不會有任何症狀，
        Beat 照樣綠。audit_logs（2A-4）落地時要加進來，這條測試到時改。"""
        assert set(PARTITIONED_TABLES) >= {"conversation_message", "platform_usagelog"}


def _month_label(months_from_now: int) -> str:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    total = now.year * 12 + (now.month - 1) + months_from_now
    return f"{total // 12:04d}_{total % 12 + 1:02d}"


class TestEnsureFuturePartitions:
    def test_every_registered_table_ends_up_covered(self) -> None:
        """斷言**狀態**（跑完之後涵蓋到第 N 個月）而不是增量（這次建了幾個）：
        同一個 worker 的 DB 是跨測試共用的，增量斷言會被執行順序左右。"""
        ensure_future_partitions(months_ahead=_FAR_AHEAD)

        expected_suffix = _month_label(_FAR_AHEAD)
        for table in PARTITIONED_TABLES:
            assert f"{table}_{expected_suffix}" in _partitions_of(table), (
                f"{table} 沒有涵蓋到 {_FAR_AHEAD} 個月後"
            )

    def test_new_partitions_are_created_with_rls(self) -> None:
        created = ensure_future_partitions(months_ahead=_FAR_AHEAD)
        # 冪等之下這次可能是空的——檢查對象是「所有現存分區」，涵蓋上一條建出來的。
        assert created == [] or created

        for table in PARTITIONED_TABLES:
            unprotected = [
                name
                for name, (enabled, forced) in _partitions_of(table).items()
                if not (enabled and forced)
            ]
            assert not unprotected, f"這些分區沒有 FORCE RLS：{unprotected}"

    def test_it_is_idempotent(self) -> None:
        ensure_future_partitions(months_ahead=_FAR_AHEAD)

        assert ensure_future_partitions(months_ahead=_FAR_AHEAD) == []

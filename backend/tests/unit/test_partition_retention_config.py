"""驗收：保留期設定 → 界線日期的換算，以及它真的被 Beat 呼叫（05 §7，2A-4 收尾）。

界線在 `prune_expired_partitions` 算：repository 收的是**明確的日期**，時間的來源
只有這一個地方（integration 測試因此不必操弄系統時鐘）。

兩件錯了都不會有例外：

1. **界線算成「今天往前推 N 個月」**。那會讓每個月的第 1 天到月底之間，界線一直
   在移動——同一個分區某天算過期、某天不算，而摘除是不可逆的（預設 DETACH 才有
   後路）。界線一律對齊**當月 1 日**再往前推。
2. **建了分區卻沒有人摘**。2A-1 的 `maintain_partitions` 只補未來、不處理過期，
   而那個缺口的症狀是「磁碟慢慢滿」，沒有任何錯誤——所以這裡驗任務真的兩件都做。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from services.platform.maintenance import cutoff_for


class TestCutoff:
    def test_it_is_aligned_to_the_first_of_the_month(self) -> None:
        """月中執行與月初執行必須得到同一個界線。"""
        first = cutoff_for(13, now=datetime(2026, 8, 1, 3, 0, tzinfo=UTC))
        middle = cutoff_for(13, now=datetime(2026, 8, 22, 23, 59, tzinfo=UTC))

        assert first == middle == date(2025, 7, 1)

    def test_it_crosses_the_year_boundary(self) -> None:
        """月份減法寫成 `month - n` 而不取模，1 月往前推會得到第 0 個月。"""
        assert cutoff_for(3, now=datetime(2026, 1, 15, tzinfo=UTC)) == date(2025, 10, 1)

    def test_seven_years_of_audit_lands_where_the_policy_says(self) -> None:
        assert cutoff_for(84, now=datetime(2026, 8, 22, tzinfo=UTC)) == date(2019, 8, 1)


class TestBeatTask:
    def test_maintaining_partitions_also_prunes_expired_ones(self, monkeypatch: Any) -> None:
        """同一個月度任務做兩件事（建未來、摘過期）：分區的生命週期是一件事，
        拆成兩個排程只會多一條「排了沒有人做」的可能。"""
        import worker.maintenance_tasks as tasks

        monkeypatch.setattr(tasks, "ensure_future_partitions", lambda months_ahead: ["created-1"])
        monkeypatch.setattr(tasks, "prune_expired_partitions", lambda: ["detached-1"])

        result = tasks.maintain_partitions()

        assert result == {"created": ["created-1"], "detached": ["detached-1"]}

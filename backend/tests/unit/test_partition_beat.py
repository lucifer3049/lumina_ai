"""驗收：分區維護的 Beat 排程（05 §5.2、04 §8.4，13 §4 工作包 2A-1）。

1D-1 建 messages 分區表時，migration 預建了 12 個月並留話：「Beat 月初預建下 3 個月
——但 Beat 目前不存在（排 2A）」。這一包把它建起來，2A-1 起 usage_logs 也是分區表，
audit_logs（2A-4）之後加入同一份名單。

**分區用完的失敗模式值得記住**：沒有涵蓋當下時間的分區時 INSERT 直接失敗（刻意
不建 DEFAULT 分區，理由見 conversation 的 0001 migration），而那發生在使用者按下
送出的當下。三道防線：migration 預建 12 個月（已有）、integration 測試在剩餘不足
3 個月時先紅（已有）、Beat 每月自動補（本包）。

本檔驗**註冊**：排程在不在、指到哪個 task、多久跑一次。分區真的建不建得出來在
tests/integration/test_partition_maintenance.py。兩層分開的理由：註冊掉了的話，
integration 全綠（函式本身是好的）而生產環境 12 個月後爆炸。
"""

from __future__ import annotations

from celery.schedules import crontab

from config.celery_app import celery_app
from core.tasks import MAINTAIN_PARTITIONS_TASK


class TestBeatRegistration:
    def test_the_maintenance_task_is_scheduled(self) -> None:
        tasks = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}

        assert MAINTAIN_PARTITIONS_TASK in tasks

    def test_it_runs_monthly_with_margin(self) -> None:
        """**每月**跑（05 §5.2），且不是排在月底最後一刻。

        它是冪等的（已存在的分區跳過），排太密只是浪費；但排「每月 31 日」這種日期
        在小月不存在，排程會安靜地整月不跑——所以釘住：月初的某一天。
        """
        entry = next(
            entry
            for entry in celery_app.conf.beat_schedule.values()
            if entry["task"] == MAINTAIN_PARTITIONS_TASK
        )
        schedule = entry["schedule"]

        assert isinstance(schedule, crontab)
        assert schedule.day_of_month == {1}

    def test_the_task_is_registered_in_the_worker(self) -> None:
        """Beat 只負責「按時把名字丟進佇列」；名字沒有對應的實作時，Beat 照樣綠，
        任務進 DLQ——兩邊都要驗。autodiscover 漏了新模組正是這種形狀的洞。"""
        import worker.maintenance_tasks  # noqa: F401 —— 觸發 task 模組載入

        assert MAINTAIN_PARTITIONS_TASK in celery_app.tasks

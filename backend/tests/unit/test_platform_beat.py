"""驗收：2A-2b 的 Beat 排程——日結對帳與 chunk 清理，以及「排了就要有人做」。

方法論同 test_partition_beat.py（註冊在 unit 驗、行為在 integration 驗），多一層
新的守門：**每一個 Beat 任務的佇列都必須被 worker 消化**。

這一條堵的是 2A-1 留下的真洞：`platform.maintain_partitions` 沒有路由，落在
default 佇列，而 `WORKER_CMD` 只聽 etl 與 embedding——Beat 會準時把任務丟進
一條沒有人聽的佇列，月復一月，直到 12 個月後分區用完。三層各自全綠：Beat 排了
（unit 有驗）、函式是對的（integration 有驗）、worker 活著——**串起來卻是斷的**，
同 1B-6「服務漏接」與 1C-4「佇列漏聽」的形狀。
"""

from __future__ import annotations

import re

from celery.schedules import crontab

from config.celery_app import celery_app
from core.tasks import (
    ANALYTICS_ROLLUP_TASK,
    CLEANUP_CHUNKS_TASK,
    MAINTAIN_PARTITIONS_TASK,
    RECONCILE_QUOTA_TASK,
    RESCUE_STUCK_DOCUMENTS_TASK,
)
from tests.unit.test_dev_launcher import _MAKEFILE  # 同一份 Makefile 快照


def _schedule_of(task_name: str) -> crontab:
    entry = next(
        entry for entry in celery_app.conf.beat_schedule.values() if entry["task"] == task_name
    )
    schedule = entry["schedule"]
    assert isinstance(schedule, crontab)
    return schedule


class TestBeatRegistration:
    def test_quota_reconciliation_runs_daily(self) -> None:
        """日結（04 §8.1）。crontab 有 day_of_month 限定的話它就不是每天。"""
        schedule = _schedule_of(RECONCILE_QUOTA_TASK)

        assert schedule.day_of_month == set(range(1, 32))

    def test_chunk_cleanup_runs_daily(self) -> None:
        schedule = _schedule_of(CLEANUP_CHUNKS_TASK)

        assert schedule.day_of_month == set(range(1, 32))

    def test_stuck_document_rescue_runs_sub_hourly(self) -> None:
        """補償掃描（enqueue 是 best-effort，訊息會丟）。頻率要密於小時級：
        停滯的文件對使用者是「上傳完就沒下文」，等一天才救太久。"""
        schedule = _schedule_of(RESCUE_STUCK_DOCUMENTS_TASK)

        assert len(schedule.minute) >= 2, "一小時內要跑不只一次"

    def test_usage_rollup_runs_hourly(self) -> None:
        """彙總每小時跑（2A-3）：Dashboard 上「今天」的數字最多晚一小時。
        每天一次的話，使用者一整天看到的都是零。"""
        schedule = _schedule_of(ANALYTICS_ROLLUP_TASK)

        assert schedule.hour == set(range(24)), "每個小時都要跑"

    def test_the_tasks_are_registered_in_the_worker(self) -> None:
        import worker.maintenance_tasks  # noqa: F401

        assert RECONCILE_QUOTA_TASK in celery_app.tasks
        assert CLEANUP_CHUNKS_TASK in celery_app.tasks
        assert RESCUE_STUCK_DOCUMENTS_TASK in celery_app.tasks
        assert ANALYTICS_ROLLUP_TASK in celery_app.tasks


class TestEveryBeatTaskHasAConsumer:
    @staticmethod
    def _worker_queues() -> set[str]:
        match = re.search(r"--queues\s+(\S+)", _MAKEFILE)
        assert match, "WORKER_CMD 找不到 --queues"
        return set(match.group(1).split(","))

    def test_every_scheduled_queue_is_consumed(self) -> None:
        """Beat 排的每一個任務，其路由佇列必須在 WORKER_CMD 的 --queues 裡。

        涵蓋現有與未來的每一條 beat_schedule——新增排程而忘了路由（或路由到沒人
        聽的佇列）時，這裡先紅，而不是等生產環境的任務安靜地堆在 broker 裡。
        """
        queues = self._worker_queues()
        routes = celery_app.conf.task_routes or {}
        for entry in celery_app.conf.beat_schedule.values():
            task = entry["task"]
            queue = routes.get(task, {}).get("queue", celery_app.conf.task_default_queue)
            assert queue in queues, f"{task} 排進了沒有人消化的佇列 {queue!r}"

    def test_maintain_partitions_is_among_them(self) -> None:
        """2A-1 的分區任務就是這個洞的第一個實例——把它點名釘住。"""
        queues = self._worker_queues()
        routes = celery_app.conf.task_routes or {}
        queue = routes.get(MAINTAIN_PARTITIONS_TASK, {}).get(
            "queue", celery_app.conf.task_default_queue
        )

        assert queue in queues

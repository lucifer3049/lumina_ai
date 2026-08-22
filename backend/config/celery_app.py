"""Celery instance 與佇列定義（02 §2、08 §1、§6）。

**佇列依資源特性分開**：ETL 吃 CPU 與記憶體（解析、切塊），embedding 吃的是外部 API
的等待時間，default 是短任務。混在同一條佇列時，一份 500 頁的 PDF 會讓所有租戶的所有
背景工作一起等——而佇列深度看起來完全正常。

**broker 與 `core.redis` 共用同一份設定**（鐵則 9）：位址寫死的話，正式環境會安靜地
連到 localhost，而症狀是「任務送出去了但永遠沒有 worker 收到」。

**不開 result backend**：ETL 的進度已經在 `etl_jobs` 與 `document.status` 裡，那才是
使用者與維運看得到的東西。再開一份 Celery 結果儲存等於同一件事有兩個會漂的來源，
而它還有 TTL——過期之後「這份文件怎麼了」只剩一個空的查詢結果。
"""

from __future__ import annotations

import os

import django
from celery import Celery
from celery.schedules import crontab

# **`django.setup()` 必須在 import 期完成**，理由與 `config/asgi.py` 相同：worker
# 啟動時會 import task → service → repository → model，而 Django model 在 import
# 當下就存取 app registry。放在 worker 的啟動 signal 裡太晚，症狀是
# ``ImproperlyConfigured: Requested setting INSTALLED_APPS``。
#
# API 行程也會 import 本模組（送任務），那時 Django 已經設定好——重複呼叫是安全的。
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from config.logging import configure_logging  # noqa: E402
from config.settings.app_settings import get_app_settings  # noqa: E402
from core.tasks import (  # noqa: E402
    ANALYTICS_ROLLUP_TASK,
    CLEANUP_CHUNKS_TASK,
    EMBED_DOCUMENT_TASK,
    INGEST_DOCUMENT_TASK,
    MAINTAIN_PARTITIONS_TASK,
    RECONCILE_QUOTA_TASK,
    RESCUE_STUCK_DOCUMENTS_TASK,
    RESCUE_STUCK_STREAMS_TASK,
    SEND_NOTIFICATION_EMAIL_TASK,
)

# worker 的日誌走與 API 同一條 pipeline（12 §1.1）。少了這行，task 內的 structlog
# 事件會退回 stdlib 預設：純文字、沒有 tenant_id、WARNING 以下全部消失。
configure_logging()

_settings = get_app_settings()

celery_app = Celery("lumina")

celery_app.conf.update(
    broker_url=_settings.redis_url.get_secret_value(),
    # 見模組 docstring：進度的單一事實來源是 DB。
    result_backend=None,
    task_default_queue="default",
    task_routes={
        INGEST_DOCUMENT_TASK: {"queue": "etl"},
        EMBED_DOCUMENT_TASK: {"queue": "embedding"},
        # 維運任務自成一條佇列（2A-2b）：塞在 etl 後面的話，大批上傳會把日結對帳
        # 推遲到不知何時。**新增 Beat 任務必須路由到 worker 有聽的佇列**——
        # tests/unit/test_platform_beat.py 對每一條 beat_schedule 驗這件事
        # （2A-1 的 maintain_partitions 曾落在沒有人聽的 default，正是這個洞）。
        MAINTAIN_PARTITIONS_TASK: {"queue": "maintenance"},
        RECONCILE_QUOTA_TASK: {"queue": "maintenance"},
        CLEANUP_CHUNKS_TASK: {"queue": "maintenance"},
        RESCUE_STUCK_DOCUMENTS_TASK: {"queue": "maintenance"},
        RESCUE_STUCK_STREAMS_TASK: {"queue": "maintenance"},
        ANALYTICS_ROLLUP_TASK: {"queue": "maintenance"},
        # 寄信（2A-5）也走 maintenance：它與維運任務一樣是「慢、可以等、不該
        # 擠在使用者的 ETL 前面」的那一類。
        SEND_NOTIFICATION_EMAIL_TASK: {"queue": "maintenance"},
    },
    # 序列化只收 JSON：pickle 能執行任意程式碼，而 broker 是一個「只要進得去就會被
    # 執行」的介面。任務參數因此一律是字串/數字（見 worker/etl_tasks.py 的 uuid 轉換）。
    task_serializer="json",
    accept_content=["json"],
    # 完成後才 ack：worker 被 OOM killer 收掉時任務要回到佇列，而不是消失。預設的
    # 「收到就 ack」會讓那份文件永遠停在 parsing，沒有錯誤也沒有人重送。安全的前提
    # 是冪等（08 §6 的 (doc_id, doc_version, stage)），那已經在 IngestionService 裡。
    task_acks_late=True,
    # 一次只抓一個：ETL 任務的長短差異極大（1 頁 vs 500 頁）。預設一次抓四個，短任務
    # 會排在長任務後面乾等，而佇列看起來是空的。
    worker_prefetch_multiplier=1,
    # worker 行程處理幾個任務後重啟。解析函式庫的記憶體不一定還得乾淨，長跑的 worker
    # 會慢慢膨脹到被 OOM killer 收掉——那時死的是**正在處理的那份文件**，與肇因無關。
    #
    # **這條只對 prefork pool 生效，而我們跑的是 threads**（理由見 Makefile 的
    # WORKER_CMD：prefork 的 daemonic 行程不准開子行程，而抽取需要）。也就是說它目前
    # 是**沒有作用的**——留著是因為切回 prefork 時仍然需要它，刪掉的話那個切換會安靜地
    # 少一道保護。
    #
    # 失去它的代價比看起來小：真正會吃記憶體的解析跑在 forkserver 子行程裡，那個行程
    # 每份文件結束就消失，且有 RLIMIT_AS 上限（etl/extract/isolated.py 的 1GB）。
    # 留在 worker 行程內的是編排與切塊，而 import 進來的解析函式庫是固定成本、不是洩漏。
    worker_max_tasks_per_child=50,
    # 送任務是**在使用者的上傳請求裡**發生的（services/knowledge/documents.py）。
    # 沒有 timeout 的話，broker 掛掉時上傳會卡到 HTTP 層逾時，而真正的問題在別處。
    broker_transport_options={"socket_timeout": 2.0, "socket_connect_timeout": 2.0},
    timezone="UTC",
    enable_utc=True,
    # Beat 排程（2A-1 起）。目前只有分區維護；日結對帳（2A-2）、統計彙總（2A-3）
    # 依序加入。**日期釘「每月 1 日」**：排 29–31 日在小月不存在，排程會安靜地
    # 整月不跑（tests/unit/test_partition_beat.py 釘住）。
    beat_schedule={
        "maintain-partitions-monthly": {
            "task": MAINTAIN_PARTITIONS_TASK,
            # 03:00 UTC：避開整點高峰與備份窗（12 §4.1 的每日 02:00 全量備份）。
            "schedule": crontab(minute=0, hour=3, day_of_month="1"),
        },
        # 2A-2b 的兩個每日維運：時間錯開（都在備份窗之後），對帳先跑——清理會刪
        # chunk，先清再對帳的話 storage 快照拍的是清理前後不一致的狀態。
        "reconcile-quota-daily": {
            "task": RECONCILE_QUOTA_TASK,
            "schedule": crontab(minute=30, hour=3),
        },
        "cleanup-chunks-daily": {
            "task": CLEANUP_CHUNKS_TASK,
            "schedule": crontab(minute=0, hour=4),
        },
        # 補償掃描每 15 分鐘：停滯對使用者是「上傳完就沒下文」，等一天太久；
        # 掃描本身是兩個帶索引的查詢，空手而回的成本近乎零。
        "rescue-stuck-documents": {
            "task": RESCUE_STUCK_DOCUMENTS_TASK,
            "schedule": crontab(minute="*/15"),
        },
        # 訊息的補償掃描同樣每 15 分鐘：症狀是「一則訊息永遠停在正在輸入」，
        # 而門檻本身已經是 10 分鐘（見 `stream_stuck_after_seconds`）。
        "rescue-stuck-streams": {
            "task": RESCUE_STUCK_STREAMS_TASK,
            "schedule": crontab(minute="*/15"),
        },
        # usage 彙總每小時（2A-3）：Dashboard 的「今天」最多晚一小時。錯開整點
        # （xx:20）避開 rescue 的 */15 與其他整點工作疊在同一秒。
        "rollup-usage-hourly": {
            "task": ANALYTICS_ROLLUP_TASK,
            "schedule": crontab(minute=20),
        },
    },
)

# task 模組**只在 worker 啟動時**才被 import（`force=False` 把它掛在 worker 的
# `import_modules` 訊號上）。
#
# 這不是最佳化，是正確性：送任務走的是名稱（`core.tasks.send_task`），發送端不需要
# 註冊表。用 `force=True` 立即 import 的話，**API 行程**會在第一次上傳時把整個 ETL
# 堆疊（pdfplumber、openpyxl、numpy…）載進來——實測讓那個請求變成 16 秒，而症狀是
# 「上傳偶爾超級慢」，看起來像物件儲存的問題。
#
# 每個 task 模組各註冊一次：`related_name` 只收一個字串，而漏掉一個模組的症狀是
# 「那條佇列的訊息堆著沒人處理」——worker 啟動成功、log 乾淨，只是它不認得那個任務名。
celery_app.autodiscover_tasks(["worker"], related_name="etl_tasks")
celery_app.autodiscover_tasks(["worker"], related_name="embedding_tasks")
celery_app.autodiscover_tasks(["worker"], related_name="maintenance_tasks")
celery_app.autodiscover_tasks(["worker"], related_name="notification_tasks")

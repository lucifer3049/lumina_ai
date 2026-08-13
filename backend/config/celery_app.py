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

from celery import Celery

from config.settings.app_settings import get_app_settings
from core.tasks import INGEST_DOCUMENT_TASK

_settings = get_app_settings()

celery_app = Celery("lumina")

celery_app.conf.update(
    broker_url=_settings.redis_url.get_secret_value(),
    # 見模組 docstring：進度的單一事實來源是 DB。
    result_backend=None,
    task_default_queue="default",
    task_routes={INGEST_DOCUMENT_TASK: {"queue": "etl"}},
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
    worker_max_tasks_per_child=50,
    # 送任務是**在使用者的上傳請求裡**發生的（services/knowledge/documents.py）。
    # 沒有 timeout 的話，broker 掛掉時上傳會卡到 HTTP 層逾時，而真正的問題在別處。
    broker_transport_options={"socket_timeout": 2.0, "socket_connect_timeout": 2.0},
    timezone="UTC",
    enable_utc=True,
)

# task 模組**只在 worker 啟動時**才被 import（`force=False` 把它掛在 worker 的
# `import_modules` 訊號上）。
#
# 這不是最佳化，是正確性：送任務走的是名稱（`core.tasks.send_task`），發送端不需要
# 註冊表。用 `force=True` 立即 import 的話，**API 行程**會在第一次上傳時把整個 ETL
# 堆疊（pdfplumber、openpyxl、numpy…）載進來——實測讓那個請求變成 16 秒，而症狀是
# 「上傳偶爾超級慢」，看起來像物件儲存的問題。
celery_app.autodiscover_tasks(["worker"], related_name="etl_tasks")

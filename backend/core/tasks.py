"""任務派送的單一入口 —— service 送任務、不認識 worker。

`services/` 需要在上傳完成後觸發 ETL，但**不得 import `worker/`**：那會讓 service
層依賴一個比它更外層的模組，而且 API 行程會因此把 task 模組（連同解析函式庫）整串
載進來。反過來 `worker/` import `services/` 是對的方向。

解法是**以名稱送任務**（`send_task`）：這裡只知道一個字串常數，實作在 worker。代價是
名稱打錯不會在 import 期被發現，因此常數定義在這裡、兩邊共用同一個。

放在 `core/` 與 `core/redis.py`、`core/object_storage.py` 一致：每一個外部系統在 repo
內只有一個入口。
"""

from __future__ import annotations

import uuid

# worker/etl_tasks.py 以這個名字註冊；config/celery_app.py 以它設定路由。
INGEST_DOCUMENT_TASK = "etl.ingest_document"
# worker/embedding_tasks.py 同理。**佇列與 ETL 分開**（06 §2 的 Q2）：兩者的資源特性
# 相反，ETL 吃 CPU 與記憶體，embedding 吃的是外部 API 的等待時間。
EMBED_DOCUMENT_TASK = "embedding.embed_document"
# worker/maintenance_tasks.py 的三個維運任務。**路由到 maintenance 佇列**
# （config/celery_app.py），而 worker 必須收聽它——2A-2b 之前 maintain_partitions
# 沒有路由、落在沒有人聽的 default 佇列（tests/unit/test_platform_beat.py 釘住）。
MAINTAIN_PARTITIONS_TASK = "platform.maintain_partitions"
# 2A-2b：quota 日結對帳（DB 蓋 Redis）與 superseded chunk 清理。
RECONCILE_QUOTA_TASK = "platform.reconcile_quota"
CLEANUP_CHUNKS_TASK = "knowledge.cleanup_chunks"
# 2A-3：usage 的每小時彙總（usage_logs → usage_daily，/analytics 的資料源）。
ANALYTICS_ROLLUP_TASK = "platform.rollup_usage"
# 停滯文件的補償掃描（enqueue 是 best-effort，訊息會丟；1B/1C 遺留的缺口）。
RESCUE_STUCK_DOCUMENTS_TASK = "knowledge.rescue_stuck_documents"
# 卡在 streaming 的訊息的補償掃描。與上面那支分開：文件的處置是「補送回佇列」，
# 訊息的處置是「就地標成中斷」——生成不是 task，沒有佇列可以補送。
RESCUE_STUCK_STREAMS_TASK = "conversation.rescue_stuck_streams"
# 二次架構審計 P0-2：軟刪除的保留窗硬刪（KB／文件／對話三種實體一起）。
# **一個 task 而不是三個**：三者的保留窗、批次上限與失敗處置完全相同，拆開只是多兩條
# 「排了卻沒有人做」的可能（2A-2b 的教訓，見 `maintain_partitions` 的註解）。
PURGE_DELETED_TASK = "platform.purge_deleted"
# 2A-5：通知的 email 派送。**不是 Beat 任務**（沒有排程），而是事件觸發——
# 寄信離開請求路徑靠的就是它。
SEND_NOTIFICATION_EMAIL_TASK = "platform.send_notification_email"


def warm_up() -> None:
    """把 Celery 的 import 與 broker 連線挪到行程啟動時。

    不做的話，**第一次上傳**要付這筆成本：實測 `import celery` 2.6s、第一次
    `send_task`（建連線）2.1s，而之後每次只要 2ms。症狀是「某些上傳偶爾非常慢」，
    而慢的那一次看起來與物件儲存或 DB 有關——那是最難查的一種效能問題：它只在
    重啟後的第一個請求出現。

    失敗不擋啟動：broker 還沒起來時 API 仍應該能服務讀取路徑（送任務本身也已經是
    best-effort，見 `enqueue_ingestion`）。
    """
    from config.logging import get_logger

    try:
        from config.celery_app import celery_app

        # `with`：`connection()` 每次都建一條新的，不關的話會外流。API 行程只跑一次
        # 感覺不出來，但測試會反覆建立 app——實測會累積上百條 Redis 連線。
        with celery_app.connection() as connection:
            connection.ensure_connection(max_retries=0, timeout=2.0)
    except Exception:
        get_logger(__name__).warning("celery_warm_up_failed", exc_info=True)


def enqueue_ingestion(
    *, tenant_id: uuid.UUID, document_id: uuid.UUID, delay_seconds: int = 0
) -> str | None:
    """把一份文件排進 etl 佇列，回傳 task id（送不出去時回 None）。

    **在交易提交之後才呼叫。** 交易內送出的話，worker 可能在 COMMIT 之前就開始處理，
    而它查不到那份文件——症狀是隨機的「文件不存在」，重跑又好。

    送不出去（broker 掛掉）**不讓上傳失敗**：文件已經在 DB 與物件儲存裡，狀態是
    `uploaded`，重跑的入口本來就存在（08 §2 的狀態機允許從失敗的階段續跑）。讓使用者
    的上傳因為背景設施而失敗，是把一個可回復的問題變成不可回復的。
    """
    return _send(
        INGEST_DOCUMENT_TASK,
        tenant_id=tenant_id,
        document_id=document_id,
        delay_seconds=delay_seconds,
    )


def enqueue_embedding(
    *, tenant_id: uuid.UUID, document_id: uuid.UUID, delay_seconds: int = 0
) -> str | None:
    """把一份已切塊的文件排進 embedding 佇列（06 §2 的 Q2），回傳 task id。

    **在 chunk 落地並提交之後才呼叫**，理由與 `enqueue_ingestion` 相同：worker 可能
    在 COMMIT 之前就開始處理，而它查到的是零個 chunk——那會把文件標成 ready，而它
    一個向量都沒有。那種錯誤不會有例外，只會讓這份文件永遠查不到。

    送不出去同樣不讓 ETL 失敗：chunk 已經在 DB 裡，文件停在 ``chunked``，重跑的入口
    存在。**代價要記著**：目前沒有掃描器會把停在 chunked 的文件撿回來（與 1B 帶進來
    的 enqueue 補償缺口是同一個，需 Celery Beat，排 2A）。
    """
    return _send(
        EMBED_DOCUMENT_TASK,
        tenant_id=tenant_id,
        document_id=document_id,
        delay_seconds=delay_seconds,
    )


def _send(
    task_name: str, *, tenant_id: uuid.UUID, document_id: uuid.UUID, delay_seconds: int = 0
) -> str | None:
    """送一個以 (tenant, document) 為參數的任務；送不出去回 None。

    兩個 enqueue 共用同一份送出邏輯——分開寫的話，`retry=False` 或例外處理總有一邊
    會漏掉，而漏掉的那一邊只在 broker 出問題時才走到（也就是沒有人測得到的時候）。
    """
    # 延後 import：`config.celery_app` 會建立 Celery instance 並讀設定，而 core 是最
    # 內圈——模組層 import 會讓每一個碰到 core 的行程（含測試蒐集）都付這筆成本。
    from config.celery_app import celery_app
    from config.logging import get_logger

    try:
        result = celery_app.send_task(
            task_name,
            kwargs={"tenant_id": str(tenant_id), "document_id": str(document_id)},
            # 公平佇列的讓位重排（2A-2b）：>0 時延後投遞，讓別的租戶先被服務。
            countdown=delay_seconds or None,
            # 不在請求路徑上重試：kombu 預設會退避重試數次，而使用者正在等上傳回應。
            # 送不出去就記 log 走人（見各 enqueue 的 docstring）——文件已經在 DB 裡。
            retry=False,
        )
    except Exception:
        get_logger(__name__).warning(
            "task_enqueue_failed",
            task=task_name,
            document_id=str(document_id),
            exc_info=True,
        )
        return None
    return str(result.id)


def enqueue_notification_email(*, tenant_id: uuid.UUID, notification_id: uuid.UUID) -> str | None:
    """把一則通知的 email 派送丟給 worker；送不出去回 None。

    **參數只有 id，不是信件內容**：內容在 DB 裡，而佇列訊息是會被序列化、被
    log、被人翻出來看的東西——通知的標題含檔名與失敗原因（10 §5 的最小揭露）。

    與 `enqueue_ingestion` 同樣是 best-effort：broker 掛掉時記 log 走人。通知
    寄不出去是小事，讓上傳或 quota 檢查因此失敗才是大事（旁路原則）。
    """
    from config.celery_app import celery_app
    from config.logging import get_logger

    try:
        result = celery_app.send_task(
            SEND_NOTIFICATION_EMAIL_TASK,
            kwargs={"tenant_id": str(tenant_id), "notification_id": str(notification_id)},
            retry=False,
        )
    except Exception:
        get_logger(__name__).warning(
            "task_enqueue_failed",
            task=SEND_NOTIFICATION_EMAIL_TASK,
            notification_id=str(notification_id),
            exc_info=True,
        )
        return None
    return str(result.id)

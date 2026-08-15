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


def enqueue_ingestion(*, tenant_id: uuid.UUID, document_id: uuid.UUID) -> str | None:
    """把一份文件排進 etl 佇列，回傳 task id（送不出去時回 None）。

    **在交易提交之後才呼叫。** 交易內送出的話，worker 可能在 COMMIT 之前就開始處理，
    而它查不到那份文件——症狀是隨機的「文件不存在」，重跑又好。

    送不出去（broker 掛掉）**不讓上傳失敗**：文件已經在 DB 與物件儲存裡，狀態是
    `uploaded`，重跑的入口本來就存在（08 §2 的狀態機允許從失敗的階段續跑）。讓使用者
    的上傳因為背景設施而失敗，是把一個可回復的問題變成不可回復的。
    """
    # 延後 import：`config.celery_app` 會建立 Celery instance 並讀設定，而 core 是最
    # 內圈——模組層 import 會讓每一個碰到 core 的行程（含測試蒐集）都付這筆成本。
    from config.celery_app import celery_app
    from config.logging import get_logger

    try:
        result = celery_app.send_task(
            INGEST_DOCUMENT_TASK,
            kwargs={"tenant_id": str(tenant_id), "document_id": str(document_id)},
            # 不在請求路徑上重試：kombu 預設會退避重試數次，而使用者正在等上傳回應。
            # 送不出去就記 log 走人（見 docstring）——文件已經在 DB 裡，重跑得回來。
            retry=False,
        )
    except Exception:
        get_logger(__name__).warning(
            "ingestion_enqueue_failed",
            document_id=str(document_id),
            exc_info=True,
        )
        return None
    return str(result.id)

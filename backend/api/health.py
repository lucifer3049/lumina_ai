"""存活與就緒探測（11 §3.2）——**刻意放在 `api/` 而不是 `api/v1/`**。

探測端點屬於**基礎設施契約**，不是業務 API——版本化的是後者。路徑寫在編排器的
設定裡（compose 的 healthcheck、K8s 的 probe），跟著 API 版本走的話，每次改版都要
同步改一份部署設定，而漏改的症狀是「容器一直被判定不健康」。

檔案位置與掛載方式必須一致，這一點有測試釘著：`tests/unit/test_openapi_export.py`
的 `test_every_v1_router_is_mounted` 要求 `api/v1/` 下的每一個 router 都真的掛在
`/api/v1` 上。本檔第一版就放在那裡，於是那條守門立刻紅——它抓到的是一個真的矛盾，
不是誤報。

兩支端點回答的是兩個不同的問題，混成一支會壞在部署那一刻：

- `/healthz`（**liveness**：這個行程還活著嗎）**不碰任何外部依賴**。編排器對
  liveness 失敗的處置是**重啟容器**，所以它只能反映「這個行程壞掉了」。把 DB 探測
  放進來的話，一次資料庫抖動會讓編排器把每一個健康的 API 容器輪流殺掉——那是把
  一個可恢復的故障放大成全面停機的標準做法。
- `/readyz`（**readiness**：現在能不能服務請求）探 DB 與 Redis。失敗的處置是
  **從負載平衡上摘掉**，不重啟——節點會在依賴恢復後自己回來。

**外部依賴（LLM provider、TEI）不納入 readiness**（11 §3.2 明文）：那些是我們控制
不了的東西，而 provider 掛掉時系統仍應該服務讀取路徑、並讓生成走降級鏈（06 §1
「降級優於失敗」）。把它們放進 readiness 等於讓一個外部服務有權摘掉我們全部的節點。

**無認證**：存活探測必須在沒有憑證的情況下打得到。因此回應內容受鐵則 9 約束——
不含主機名、埠、連線字串、版本以外的任何內部拓撲。`orm_runtime_knobs()` 的兩個值
（threadpool 寬度、連線壽命）是刻意選過的診斷值，理由見 `core/db.py`。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from api.background import pending_count
from config.logging import get_logger
from core.db import orm_runtime_knobs, run_orm
from core.health import probe_database, probe_redis

logger = get_logger(__name__)

router = APIRouter(tags=["health"], include_in_schema=False)


@router.get("/healthz", operation_id="health_live")
async def healthz() -> dict[str, Any]:
    """存活探測。**永遠回 200**——只要這個行程還能跑到這一行，它就是活著的。

    順帶回報進行中的背景生成數：關機演練時，那是「drain 有沒有真的在等」唯一
    看得到的數字（`api/background.py`），而它不需要碰任何依賴就拿得到。
    """
    return {"status": "ok", "generations_in_flight": pending_count(), **orm_runtime_knobs()}


@router.get("/readyz", operation_id="health_ready")
async def readyz(response: Response) -> dict[str, Any]:
    """就緒探測：DB 與 Redis 可達即 200，任一不可達回 **503**。

    **回 503 而不是 raise**：這支端點的消費者是編排器，它只看狀態碼。走
    `DomainError` 那條路會把一次依賴故障寫成 ERROR 級的 `domain_error` 日誌，
    而 readiness 每幾秒就打一次——依賴掛掉的那幾分鐘會產生幾百筆 ERROR，把真正
    需要人看的事件淹掉（12 §1.1 的等級紀律）。

    **不回傳失敗原因給呼叫端**：那會洩漏內部拓撲（連線字串、主機名）。原因寫進
    log，那裡有 request_id 可以對照。
    """
    checks = {"database": await run_orm(probe_database), "redis": await run_orm(probe_redis)}
    ready = all(checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning("readiness_probe_failed", **checks)
    return {"status": "ok" if ready else "unavailable", "checks": checks}

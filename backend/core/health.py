"""依賴可達性探測 —— `/readyz` 的兩個判斷（11 §3.2）。

**放 `core/` 而不是 endpoint 裡**：兩個探測都要碰基礎設施（DB 連線、Redis client），
而鐵則 3 的 controller 不碰基礎設施。這裡與 `core/redis.py`、`core/object_storage.py`
同一個角色——每個外部系統在 repo 內只有一個入口。

**兩個探測都不拋例外**：呼叫端要的是「可不可用」這個布林值，而不是一個要接的例外。
把連線失敗轉成 False 在這裡做一次，好過在每個呼叫端各寫一次 try。
"""

from __future__ import annotations

from django.db import connection

from config.logging import get_logger
from core.redis import get_redis

logger = get_logger(__name__)

__all__ = ["probe_database", "probe_redis"]


def probe_database() -> bool:
    """DB 可達即 True。

    **`SELECT 1` 而不是查任何一張表**：探測不該依賴 schema（migration 還沒跑完時
    readiness 本來就該是 False，但那要由 migration job 的順序保證，不是由一個
    會因為表不存在而永遠失敗的探測來表達）。也不該碰租戶資料——這條路徑沒有
    TenantContext，任何 tenant-scoped 查詢在 RLS 之下都會 fail closed。

    **同步函式**：由 `run_orm` 從 threadpool 呼叫（ADR-001）。它同時因此驗到了
    「橋接還活著」——連線池耗盡時這裡會逾時，而那正是該從 LB 上摘掉的狀態。
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            return cursor.fetchone() is not None
    except Exception:
        logger.warning("database_probe_failed", exc_info=True)
        return False


def probe_redis() -> bool:
    """Redis 可達即 True。

    `PING` 而不是讀寫一個 key：探測不該留下垃圾，也不該因為某個 key 的狀態而失敗。
    client 本身帶 timeout（`redis_timeout_seconds`），所以這裡不會掛住探測。
    """
    try:
        return bool(get_redis().ping())
    except Exception:
        logger.warning("redis_probe_failed", exc_info=True)
        return False

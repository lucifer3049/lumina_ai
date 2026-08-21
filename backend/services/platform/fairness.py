"""per-tenant 公平佇列的並發閘（08 §6 背壓，1B 缺口④，2A-2b）。

etl／embedding 佇列是 FIFO：單一租戶倒 500 份文件進來，別人的一份小文件就排在
500 份後面（鄰居效應），而每個系統指標都是綠的。閘做在 **worker 端**：task 開跑
前取該租戶的 slot，拿不到就讓位（重新排隊帶延遲，見 worker/*_tasks.py）——
上傳永遠不因佇列滿而失敗（1B-3），推遲的是處理、不是收件。

slot 是 Redis 計數（`t:{tenant}:slots:{kind}`，鐵則 4 前綴），帶安全 TTL：worker
被 OOM 砍掉時佔位自然消失，否則那個租戶的 ETL 會慢慢降到零。TTL 的代價是超長
任務（> TTL）會讓閘短暫超admit——公平閘往**寬鬆**倒是對的方向：它保護的是別的
租戶的等待時間，不是資源安全。
"""

from __future__ import annotations

import uuid
from typing import cast

from config.settings.app_settings import get_app_settings
from core.redis import get_redis, tenant_key

__all__ = ["TenantSlotLimiter"]

_SLOT_TTL_SECONDS = 3600


class TenantSlotLimiter:
    """一種佇列（kind：etl / embedding）的租戶並發閘。"""

    def __init__(self, kind: str) -> None:
        self._kind = kind

    def _key(self, tenant_id: uuid.UUID) -> str:
        return tenant_key(tenant_id, "slots", self._kind)

    def acquire(self, tenant_id: uuid.UUID) -> bool:
        """要一個 slot。拿不到回 False——**被拒絕的那一次不佔位**（先加後退，
        同 quota 計數器的原子擋線）。"""
        cap = get_app_settings().etl_max_concurrent_per_tenant
        client = get_redis()
        key = self._key(tenant_id)
        active = cast("int", client.incr(key))
        client.expire(key, _SLOT_TTL_SECONDS)
        if active > cap:
            client.decr(key)
            return False
        return True

    def release(self, tenant_id: uuid.UUID) -> None:
        """歸還。重複歸還不會讓計數變負（負數＝送別人插隊的權利）。"""
        client = get_redis()
        remaining = cast("int", client.decr(self._key(tenant_id)))
        if remaining < 0:
            client.incr(self._key(tenant_id))

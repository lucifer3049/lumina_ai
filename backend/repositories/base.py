"""Repository 基底 —— 租戶隔離的唯一實施點（ADR-002）。

鐵則 4：所有 Repository 繼承 :class:`TenantScopedRepository`，tenant filter
由基底自動注入，子類別不得自行組 queryset 起點。TenantContext 缺失一律
raise（Fail Fast），不提供「預設租戶」或「無租戶模式」的退路。

游標分頁的兩個 helper（:func:`cursor_key`、:func:`split_page`）也住這裡：對話與稽核
（2A-4）用的是同一套「多取一筆 + (時間, id) 複合鍵」，而它原本只在
``repositories/conversation.py``。放這裡的理由同 :class:`SoftDeletableRepository`
——它是跨 context 的共用基礎設施，不屬於任何一個 bounded context。

**PgBouncer transaction mode 的限制**（05 §5.5）：本層禁用 session 級功能
——advisory lock、``SET``（``SET LOCAL`` 在交易內安全）、server-side prepared
statement（已於 settings 以 ``prepare_threshold=None`` 關閉）、以及 **server-side
cursor**（``QuerySet.iterator()``；已於 settings 以
``DISABLE_SERVER_SIDE_CURSORS=True`` 關閉，理由見該處）。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from django.db.models import Model, QuerySet
from django.utils import timezone

from common.cursors import CursorError
from core.tenant import get_current_tenant_id


class TenantScopedRepository[M: Model]:
    """所有 tenant-scoped 資料存取的起點。"""

    model: type[M]

    def get_queryset(self) -> QuerySet[M]:
        """已套用 tenant filter 的 queryset。

        子類別的每一個查詢都必須由此出發——直接用 ``self.model.objects``
        會繞過租戶隔離。
        """
        tenant_id = get_current_tenant_id(
            operation=f"{type(self).__name__}.get_queryset",
        )
        return self.model._default_manager.filter(tenant_id=tenant_id)


# **1D-1 從 `repositories/knowledge.py` 搬過來。** 軟刪除是 05 §5.4 的跨 context 規則
# （KB、document、conversation、prompt 都適用），放在某一個 context 的檔案裡，第二個
# 需要它的 context 就得 import 那個 context——而 ADR-006 的 bounded context 之間不該
# 互相依賴。這裡是共用基礎設施，不是任何一個 context。


class SoftDeletableRepository[M: Model](TenantScopedRepository[M]):
    """軟刪除的實體：預設查詢排除已刪除的列。

    覆寫 ``get_queryset`` 而不是要求每個查詢自己加條件——後者只要有一處漏掉，
    使用者就會看到已刪除的資料，而那一處不會有任何症狀。要撈已刪除的列（清理
    worker、還原功能）走 :meth:`including_deleted`，讓那個意圖在呼叫端顯式可見。
    """

    def get_queryset(self) -> QuerySet[M]:
        return super().get_queryset().filter(deleted_at__isnull=True)

    def including_deleted(self) -> QuerySet[M]:
        """含已刪除的列——只給清理 worker 與還原流程用。"""
        return super().get_queryset()

    def soft_delete(self, entity_id: uuid.UUID) -> int:
        """標記刪除；回傳影響的列數（0 = 不存在或已刪除）。

        不硬刪（05 §5.4）：硬刪要級聯 embeddings → chunks → documents，那是清理
        worker 分批做的事，在請求路徑上做會鎖表。
        """
        return self.get_queryset().filter(id=entity_id).update(deleted_at=timezone.now())


def cursor_key(cursor: dict[str, Any]) -> tuple[datetime, uuid.UUID]:
    """游標 dict → (排序鍵, id)。內容壞掉一律 `CursorError`。

    游標可能來自舊版前端存的 localStorage、被截斷的網址、或使用者手改——因此
    **不能假設欄位存在或型別正確**，而錯誤要是呼叫端轉得成 422 的那一種。
    """
    try:
        return datetime.fromisoformat(str(cursor["k"])), uuid.UUID(str(cursor["id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise CursorError("游標內容不正確") from exc


def split_page[T](
    rows: list[T], *, limit: int, cursor_of: Callable[[T], dict[str, Any]]
) -> tuple[list[T], dict[str, Any] | None]:
    """把「多取一筆」的結果切成一頁 + 下一頁的游標。

    **多取一筆是判斷「還有沒有下一頁」唯一可靠的方法**。用 `count()` 另外查一次的話，
    兩次查詢之間新增的資料會讓「有沒有下一頁」與實際內容對不上；而只憑「這頁滿了」
    來推斷，會在資料剛好是頁大小整數倍時多給一個永遠回空清單的游標。

    `cursor_of` 由呼叫端提供而不是在這裡讀欄位：排序鍵每個查詢不同，而
    `Conversation` 的鍵是 `COALESCE(last_message_at, created_at)`——在 Python 端算
    （`a or b`）與 SQL 的 COALESCE 等價，且不必把 annotate 出來的欄位帶進型別系統。
    """
    if len(rows) <= limit:
        return rows, None
    page = rows[:limit]
    return page, cursor_of(page[-1])

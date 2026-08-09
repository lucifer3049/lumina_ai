"""TenantContext —— 以 contextvars 承載當前租戶（ADR-002）。

為什麼是 contextvars 而不是全域變數或 thread-local：

- **全域變數**：所有請求共用一份，併發下必然串租戶。
- **thread-local**：async 世界裡一條執行緒交錯跑多個 coroutine，thread-local
  會在 await 之間被別的請求覆寫；反過來 ``sync_to_async`` 又會換到別的執行緒，
  值直接消失。
- **contextvars**：綁定的是「執行上下文」而非執行緒。asgiref 的 ``sync_to_async``
  會把當前 context 複製進 threadpool 執行緒——這正是 ADR-001 橋接能保住租戶
  隔離的前提，也是 spike 要驗證的行為之一（tests/integration/test_tenant_scope.py）。

缺 context 一律 raise（Fail Fast）：不提供預設租戶、不提供「無租戶模式」。

**型別是 ``uuid.UUID | None`` 而不是「未設定就是 LookupError」**（1A-5 改）：
需要一個「明確清空」的動作。租戶由 route 層的認證 ``Depends`` 設定，設定者拿不到
一個能涵蓋整個請求生命週期的 ``finally``——還原只能由請求層的 middleware 做，而它
在請求開始時手上沒有任何 token 可以 reset。用 ``None`` 當空值即可讓
:func:`clear_current_tenant_id` 成立。``get`` 的語意完全不變：``None`` 一樣 raise。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from core.exceptions import TenantContextMissingError

_current_tenant_id: ContextVar[uuid.UUID | None] = ContextVar("current_tenant_id", default=None)


def get_current_tenant_id(*, operation: str | None = None) -> uuid.UUID:
    """取得當前租戶 id；未設定則 raise ``TenantContextMissingError``。"""
    tenant_id = _current_tenant_id.get()
    if tenant_id is None:
        raise TenantContextMissingError(operation)
    return tenant_id


def try_get_current_tenant_id() -> uuid.UUID | None:
    """不 raise 的版本——僅供 log/metrics 這類「沒有也要能繼續」的場合使用。

    業務程式碼一律用 :func:`get_current_tenant_id`。
    """
    return _current_tenant_id.get()


def set_current_tenant_id(tenant_id: uuid.UUID) -> Token[uuid.UUID | None]:
    return _current_tenant_id.set(tenant_id)


def reset_current_tenant_id(token: Token[uuid.UUID | None]) -> None:
    _current_tenant_id.reset(token)


def clear_current_tenant_id() -> None:
    """清空當前租戶——**請求結束時的收尾**（api/main.py 的 middleware）。

    為什麼不能省：contextvars 的隔離來自「每個 task 拿到一份 context 副本」，而那
    只在真的**另起一個 task** 時成立。同一個 context 內連續處理兩個請求時（測試的
    ASGITransport、未來任何在單一 task 內重用的路徑），前一個請求的租戶會留給下一
    個——症狀是 log 標到別的租戶，而查詢會在 RLS 之下讀到那個租戶的資料。
    """
    _current_tenant_id.set(None)


@contextmanager
def tenant_context(tenant_id: uuid.UUID) -> Iterator[uuid.UUID]:
    """在 with 區塊內綁定租戶，離開時還原（巢狀安全）。

    HTTP 路徑由 ``api/dependencies/auth.py`` 從已驗證的 JWT claim 設定；此 helper
    供 worker、腳本與測試使用。
    """
    token = set_current_tenant_id(tenant_id)
    try:
        yield tenant_id
    finally:
        reset_current_tenant_id(token)

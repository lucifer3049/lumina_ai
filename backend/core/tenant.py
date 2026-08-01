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
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from core.exceptions import TenantContextMissingError

_current_tenant_id: ContextVar[uuid.UUID] = ContextVar("current_tenant_id")


def get_current_tenant_id(*, operation: str | None = None) -> uuid.UUID:
    """取得當前租戶 id；未設定則 raise ``TenantContextMissingError``。"""
    try:
        return _current_tenant_id.get()
    except LookupError as exc:
        raise TenantContextMissingError(operation) from exc


def try_get_current_tenant_id() -> uuid.UUID | None:
    """不 raise 的版本——僅供 log/metrics 這類「沒有也要能繼續」的場合使用。

    業務程式碼一律用 :func:`get_current_tenant_id`。
    """
    return _current_tenant_id.get(None)


def set_current_tenant_id(tenant_id: uuid.UUID) -> Token[uuid.UUID]:
    return _current_tenant_id.set(tenant_id)


def reset_current_tenant_id(token: Token[uuid.UUID]) -> None:
    _current_tenant_id.reset(token)


@contextmanager
def tenant_context(tenant_id: uuid.UUID) -> Iterator[uuid.UUID]:
    """在 with 區塊內綁定租戶，離開時還原（巢狀安全）。

    正式路徑由 ``api/middleware/tenant_context.py`` 設定；此 helper 供
    worker、腳本與測試使用。
    """
    token = set_current_tenant_id(tenant_id)
    try:
        yield tenant_id
    finally:
        reset_current_tenant_id(token)

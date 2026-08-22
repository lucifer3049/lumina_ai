"""稽核的請求層 context（04 §8.3，2A-4）——middleware 看不到的那一半。

middleware 知道**誰、何時、從哪、對哪個端點、結果如何**；它不知道**改成什麼**，
也不知道建立出來的資源是哪一個（id 在回應裡，不在 URL 上）。那兩件事只有 service
知道，而 service 不該為了稽核多一個回傳值——那會讓每個簽章都被稽核污染。

因此用與 `core/tenant.py` 同一個機制：contextvar。middleware 在請求開始時開一個
scope，service 在它自己的交易裡呼叫 :func:`describe` 往上填，middleware 在回應
之後把整包寫成一列。

**scope 不存在時 `describe` 是 no-op**：worker、CLI、測試都會呼叫到那些 service，
而它們沒有請求。稽核在那些路徑上的正確行為是「不記」——不是報錯，也不是記一列
沒有來源的紀錄。

`before`／`after` 一律**白名單欄位**，由呼叫端自己挑（不是「整個物件減幾欄」）：
黑名單漏一個就是明文外洩，而欄位是持續加的（10 §5）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class AuditScope:
    """一次請求的稽核材料。前三個由 middleware 填、其餘由 service 補。"""

    ip: str | None
    user_agent: str
    request_id: str
    resource_id: uuid.UUID | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    # 被拒的 permission code（10 §3）。由 `RequireScope` 在 raise 之前填。
    permission: str | None = None


_current_scope: ContextVar[AuditScope | None] = ContextVar("audit_scope", default=None)


@contextmanager
def scope(*, ip: str | None, user_agent: str, request_id: str) -> Iterator[AuditScope]:
    """在 with 區塊內開一個稽核 scope（middleware 專用）。

    離開時還原而不是清空：巢狀安全，且測試可以在同一個 context 內連續跑多個請求。
    """
    current = AuditScope(ip=ip, user_agent=user_agent, request_id=request_id)
    token = _current_scope.set(current)
    try:
        yield current
    finally:
        _current_scope.reset(token)


def describe(
    *,
    resource_id: uuid.UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    permission: str | None = None,
) -> None:
    """補上 middleware 看不到的欄位。沒有 scope 時什麼都不做。

    只覆寫「有給值」的欄位——同一次請求可能有兩個呼叫點各補一半
    （例如 service 給 before/after、dependency 給 permission）。
    """
    current = _current_scope.get()
    if current is None:
        return
    if resource_id is not None:
        current.resource_id = resource_id
    if before is not None:
        current.before = before
    if after is not None:
        current.after = after
    if permission is not None:
        current.permission = permission


def current_scope() -> AuditScope | None:
    """目前的稽核 scope；不在請求內時為 None。

    給「請求層記不了、只能自己記」的 service 用（登入：沒有 principal、也沒有
    租戶 contextvar），它需要 ip／user_agent／request_id。
    """
    return _current_scope.get()

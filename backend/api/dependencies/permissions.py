"""權限宣告 —— `RequireScope`（10 §3）。

**權限宣告在端點上可見**是刻意的設計（10 §3）：讀 router 就知道每個端點需要什麼
權限，而且可以自動彙整成權限矩陣文件。把判定藏在 service 裡的話，「這個端點到底
要什麼權限」只能靠追程式碼回答，而漏掉一個端點不會有任何症狀。

**被拒的請求要留紀錄且帶上被拒的 code**（10 §3）：少了它，「有人在嘗試越權」與
「某個角色的權限設錯了」在維運端看起來完全一樣——都只是使用者說「我打不開」。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends

from api.dependencies.auth import Principal, require_authenticated
from config.logging import get_logger
from core import audit
from core.exceptions import PermissionDeniedError
from services.identity.permissions import PermissionService

logger = get_logger(__name__)
_permissions = PermissionService()


def RequireScope(code: str) -> Callable[[Principal], Awaitable[Principal]]:  # noqa: N802
    """回傳一個 dependency：通過認證**且**具備 ``code`` 才放行。

    命名刻意用大寫開頭（10 §3 的寫法 ``RequireScope("knowledge:write")``）：
    它在 router 上讀起來像宣告而不是函式呼叫，而那正是它的用途。
    """

    async def dependency(
        principal: Annotated[Principal, Depends(require_authenticated)],
    ) -> Principal:
        if not _permissions.has(principal.roles, code):
            logger.warning(
                "permission_denied",
                required_permission=code,
                roles=list(principal.roles),
                user_id=str(principal.user_id),
            )
            # 稽核也要拿到被拒的 code（10 §3）：log 是給維運看的，稽核是給
            # 租戶的管理者看的，兩邊的讀者不同。middleware 只看得到 403，
            # 「被哪一個權限擋下」只有這裡知道。
            audit.describe(permission=code)
            raise PermissionDeniedError(required=code)
        return principal

    return dependency

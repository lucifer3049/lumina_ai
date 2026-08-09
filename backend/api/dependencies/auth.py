"""認證 dependency —— 租戶身分的唯一合法來源（ADR-002、09 §1）。

**租戶只從已驗證的 JWT claim 取得，永不接受 client 自報。** 唯一曾經違反這條的
是 spike 期的 ``X-Tenant-Id`` 標頭（無認證壓測用），已於 1A-5 整組刪除；
`tests/api/test_auth_endpoints.py` 仍留著一條「帶標頭也不影響身分」的測試，防的是
日後有人再接一條類似的捷徑。

**這裡也是 contextvar 的設定點**：租戶一旦設進 contextvar，Repository 的 tenant
filter 與 UoW 送給 RLS 的 ``app.tenant_id`` 就都有值了。它跑在 route 內（比所有
middleware 都晚），這正是 13 §3.2 要求把 log 的租戶改成「emit 時才讀」的原因。
**還原不在這一層**：dependency 沒有涵蓋整個請求的 finally，收尾由
``api/main.py`` 的 ``RequestContextMiddleware`` 做（``clear_current_tenant_id()``）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import Request

from core.db import run_orm
from core.exceptions import AuthenticationError, ErrorCode
from core.tenant import set_current_tenant_id
from services.identity.auth import AuthService

_service = AuthService()


@dataclass(frozen=True)
class Principal:
    """通過認證的呼叫者。1A-4 的權限判定會以此為輸入。

    帶著 ``jti`` 與 ``access_expires_at`` 是為了讓登出能撤銷「這一張」token：
    撤銷名單的 TTL 要設成剩餘壽命，否則要嘛提早失效、要嘛永久佔著記憶體。
    """

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    roles: tuple[str, ...]
    jti: uuid.UUID
    access_expires_at: datetime


class _MissingCredentialsError(AuthenticationError):
    code = ErrorCode.AUTH_REQUIRED


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _MissingCredentialsError("需要 Bearer token")
    return token


async def require_authenticated(request: Request) -> Principal:
    """驗證 access token，設定租戶 contextvar，回傳 :class:`Principal`。

    走 ``run_orm``：驗證會碰 Redis（撤銷名單），而那是同步 client——直接在 event
    loop 上呼叫會阻塞整個 loop（ADR-001 的同一條理由）。

    contextvar 的設定**不還原**：ASGI 的每個請求跑在自己的 context 副本裡，
    離開時自動丟棄；而 `api/main.py` 的 middleware 另外有一次清理。在這裡 reset
    反而會讓後續（例如例外處理與存取日誌）讀不到租戶。
    """
    token = _bearer_token(request)
    claims = await run_orm(_service.describe_access_token, token)

    set_current_tenant_id(claims.tenant_id)
    return Principal(
        user_id=claims.sub,
        tenant_id=claims.tenant_id,
        roles=claims.roles,
        jti=claims.jti,
        access_expires_at=claims.expires_at,
    )

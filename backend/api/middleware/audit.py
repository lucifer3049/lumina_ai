"""稽核 middleware——寫入型請求自動留痕（04 §8.3、10 §3，2A-4）。

**預設全記，豁免要明講**（開工前人類核可的觸發面）：任何 POST/PUT/PATCH/DELETE
只要走到有 `operation_id` 的路由就會產生一列，除非它在 :data:`AUDIT_EXEMPT`。
方向是 fail-safe——新端點忘了進註冊表仍然會被記下來（action 退回 operation_id），
反過來的設計（要宣告才記）漏一個就是完全沒有紀錄，而那沒有任何症狀。
分類本身由 `tests/unit/test_audit_registry.py` 的手寫清單守門。

**位置必須在 `RequestContextMiddleware` 裡面**：它讀租戶 contextvar（由 route 層的
認證 `Depends` 設定），而那個 contextvar 在外層的 finally 被清掉。掛反了會一列都
寫不出去，且使用者端完全正常。

**寫入時機是回應送完之後**：`await self.app(...)` 回來時 body 已經送出，稽核的
DB 往返不落在使用者感受得到的延遲上。代價是「回應已經給了、稽核才失敗」——這正是
`AuditService.record` 吞例外的理由（旁路原則）。
"""

from __future__ import annotations

import uuid
from typing import NamedTuple

from fastapi import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.middleware.request_context import request_id_of
from config.logging import get_logger
from core import audit
from core.db import run_orm
from core.tenant import try_get_current_tenant_id
from services.platform.audit import (
    OUTCOME_DENIED,
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    AuditEvent,
    AuditService,
)

logger = get_logger(__name__)

__all__ = ["AUDIT_ACTIONS", "AUDIT_EXEMPT", "RECORDED_BY_SERVICE", "AuditMiddleware", "AuditSpec"]

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class AuditSpec(NamedTuple):
    """一個端點的稽核宣告。

    `path_param` 是 `None` 代表資源 id 不在 URL 上（建立類的 id 在回應裡、自助類
    的主體就是呼叫者自己）——那時由 service 經 `core/audit.py` 的 `describe()` 補。
    """

    action: str
    resource_type: str
    path_param: str | None


# operation_id → 宣告。**新增寫入型端點時必須同步這裡或豁免清單**，
# 否則 tests/unit/test_audit_registry.py 會紅。
AUDIT_ACTIONS: dict[str, AuditSpec] = {
    "users_create": AuditSpec("user.create", "user", None),
    "users_update": AuditSpec("user.update", "user", "user_id"),
    "users_update_me": AuditSpec("user.update", "user", None),
    "users_deactivate": AuditSpec("user.deactivate", "user", "user_id"),
    "auth_change_password": AuditSpec("user.change_password", "user", None),
    "auth_logout": AuditSpec("auth.logout", "session", None),
    "tenants_update_current": AuditSpec("tenant.update", "tenant", None),
    "knowledge_bases_create": AuditSpec("knowledge_base.create", "knowledge_base", None),
    "knowledge_bases_update": AuditSpec("knowledge_base.update", "knowledge_base", "kb_id"),
    "knowledge_bases_delete": AuditSpec("knowledge_base.delete", "knowledge_base", "kb_id"),
    "documents_upload": AuditSpec("document.upload", "document", None),
    "documents_reingest": AuditSpec("document.reingest", "document", "document_id"),
    "documents_delete": AuditSpec("document.delete", "document", "document_id"),
}

# 豁免——**高頻且非管理面**。每一條的理由見 tests/unit/test_audit_registry.py。
AUDIT_EXEMPT: frozenset[str] = frozenset(
    {
        "auth_refresh",
        "conversations_create",
        "conversations_update",
        "conversations_delete",
        "conversations_send_message",
        "conversations_stop_message",
        "rag_query",
        # 標已讀（2A-5）：本人對自己收件匣的狀態變更、高頻，且不涉及任何
        # 他人看得見的資源。記下來只會把真正的敏感操作淹掉。
        "notifications_mark_read",
    }
)

# 請求層記不了、由 service 自己記的（登入：沒有 principal，也沒有租戶 contextvar）。
RECORDED_BY_SERVICE: frozenset[str] = frozenset({"auth_login"})


class AuditMiddleware:
    """純 ASGI（理由同 `RequestContextMiddleware`：contextvar 要在同一個 task）。"""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self._service = AuditService()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive)
        status_seen = 500

        async def watch_status(message: Message) -> None:
            nonlocal status_seen
            if message["type"] == "http.response.start":
                status_seen = int(message["status"])
            await send(message)

        with audit.scope(
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent", ""),
            request_id=request_id_of(request),
        ) as current:
            try:
                await self._app(scope, receive, watch_status)
            except Exception:
                # 例外要到外層才變成 500 回應。不在這裡記就會出現「操作真的發生過、
                # 但稽核上只看得到成功的那些」——最需要紀錄的那一類反而沒有。
                await self._record(scope, current, status=500)
                raise
            await self._record(scope, current, status=status_seen)

    async def _record(self, scope: Scope, current: audit.AuditScope, *, status: int) -> None:
        """把這次請求寫成一列。**任何失敗都只記 log**（旁路原則）。"""
        if scope["method"] not in WRITE_METHODS:
            return

        route = scope.get("route")
        operation_id = getattr(route, "operation_id", None)
        if operation_id in AUDIT_EXEMPT or operation_id in RECORDED_BY_SERVICE:
            return

        tenant_id = try_get_current_tenant_id()
        if tenant_id is None:
            # 401（沒有通過認證）與路由不存在都走到這裡。沒有租戶就沒有地方放這一列
            # （tenant_id NOT NULL + RLS），硬記只能記進別人的帳。真正需要的那一半
            # ——登入失敗——由 AuthService 記，它知道租戶。
            return

        spec = AUDIT_ACTIONS.get(operation_id) if operation_id else None
        if spec is None:
            # 沒宣告仍然記（fail-safe）：名字粗糙好過完全沒有紀錄。
            # 正常情況下走不到——守門測試逼每條寫入型路由二選一。
            logger.warning("audit_action_unregistered", operation_id=operation_id)
            spec = AuditSpec(f"unregistered.{operation_id or 'unknown'}", "unknown", None)

        # 認證 `Depends` 把 principal 留在 `request.state`（Starlette 存進
        # `scope["state"]`）：middleware 拿不到 route 的回傳值，而 contextvar 只
        # 承載租戶（`core/tenant.py`），不承載使用者。
        principal = (scope.get("state") or {}).get("principal")
        await run_orm(
            self._service.record,
            tenant_id,
            AuditEvent(
                action=spec.action,
                resource_type=spec.resource_type,
                outcome=_outcome(status),
                request_id=current.request_id,
                actor_id=getattr(principal, "user_id", None),
                actor_type="user" if principal is not None else "system",
                resource_id=current.resource_id or _path_resource_id(scope, spec),
                before=current.before,
                after=current.after,
                status=status,
                permission=current.permission,
                ip=current.ip,
                user_agent=current.user_agent,
            ),
        )


def _outcome(status: int) -> str:
    if status == 403:
        return OUTCOME_DENIED
    return OUTCOME_SUCCEEDED if status < 400 else OUTCOME_FAILED


def _path_resource_id(scope: Scope, spec: AuditSpec) -> uuid.UUID | None:
    """從 URL 取資源 id。**宣告哪個參數而不是猜**：`documents_upload` 的 URL 上
    有 `kb_id`，但被建立的資源是 document——猜的話每一列都指錯對象。"""
    if spec.path_param is None:
        return None
    raw = (scope.get("path_params") or {}).get(spec.path_param)
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError):
        return None

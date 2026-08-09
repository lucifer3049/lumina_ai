"""TenantService —— 租戶開通（04 §Tenant、13 §4）。

**租戶與第一個 Owner 必須一起建立。** 只建租戶會產生一個沒有任何使用者的空殼：
沒有人登得進去，也就沒有人能在裡面建立第一個使用者——死結，而症狀是「租戶明明
開通了，客戶說登不進去」。所以本模組沒有「只建租戶」的公開方法。

**建立流程本身要在該租戶的 context 內執行**：`identity_tenant` 的 RLS policy 是
``id = current_setting('app.tenant_id')``（1A-2），所以順序是「先產生 uuid → 設進
context → 寫入」。這不是繞過隔離，而是隔離的必然結果——系統裡沒有「站在所有租戶
之外」的位置（我們刻意不建 BYPASSRLS 角色，見 core/uow.py）。

登入用的 slug → id 目錄由 **DB trigger** 維護（migration 0004），不在這裡寫：
交給應用程式的話，任何一條沒走本模組的建立路徑都會漏一列。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from django.db import IntegrityError

from common.passwords import hash_password
from core.exceptions import ConflictError, NotFoundError
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import RoleRepository, TenantRepository, UserRepository
from services.identity.permissions import SystemRole


@dataclass(frozen=True)
class TenantView:
    """對外的租戶表示。

    與 `UserView` 同一個理由：明確列欄位，而不是把 model 丟給上層序列化——
    後者只要有人在 model 上加一個內部欄位（例如計費註記）就會自動外流。
    """

    id: uuid.UUID
    name: str
    slug: str
    plan: str
    status: str


@dataclass(frozen=True)
class TenantCreated:
    tenant_id: uuid.UUID
    owner_user_id: uuid.UUID
    slug: str


class TenantService:
    def __init__(
        self,
        *,
        tenants: TenantRepository | None = None,
        users: UserRepository | None = None,
        roles: RoleRepository | None = None,
    ) -> None:
        self._tenants = tenants or TenantRepository()
        self._users = users or UserRepository()
        self._roles = roles or RoleRepository()

    def create_tenant(
        self,
        *,
        name: str,
        slug: str,
        owner_email: str,
        owner_password: str,
    ) -> TenantCreated:
        tenant_id = uuid.uuid4()

        with tenant_context(tenant_id), unit_of_work():
            try:
                self._tenants.create(tenant_id=tenant_id, name=name, slug=slug)
            except IntegrityError as exc:
                # slug 是登入的識別字，重複等於兩家公司搶同一個入口。
                raise ConflictError(f"slug `{slug}` 已被使用") from exc

            owner = self._users.create(
                email=owner_email,
                display_name=owner_email.split("@")[0],
                password_hash=hash_password(owner_password),
                tenant_id=tenant_id,
            )
            self._roles.assign(
                user_id=owner.id, role_name=SystemRole.OWNER.value, tenant_id=tenant_id
            )

        return TenantCreated(tenant_id=tenant_id, owner_user_id=owner.id, slug=slug)

    # ── 本租戶資訊 ──────────────────────────────────────────────

    def get_current(self, tenant_id: uuid.UUID) -> TenantView:
        with tenant_context(tenant_id), unit_of_work():
            tenant = self._tenants.current()
            if tenant is None:
                # token 的租戶在 DB 裡不存在——資料被刪或 token 屬於已刪除的租戶。
                raise NotFoundError("租戶不存在")
            return self._view(tenant)

    def update_current(self, tenant_id: uuid.UUID, *, name: str | None = None) -> TenantView:
        with tenant_context(tenant_id), unit_of_work():
            if name is not None:
                self._tenants.rename(name)
            tenant = self._tenants.current()
            if tenant is None:
                raise NotFoundError("租戶不存在")
            return self._view(tenant)

    def _view(self, tenant: Any) -> TenantView:
        # 參數型別是 Any：`services/` 依鐵則 2 不得 import `apps.*`，所以這裡看不到
        # Django model 的型別。轉成 dataclass 之後上層就有完整型別了。
        return TenantView(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            plan=tenant.plan,
            status=tenant.status,
        )

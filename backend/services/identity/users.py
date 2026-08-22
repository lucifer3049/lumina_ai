"""UserService —— 使用者的建立、查詢、更新、停用、改密碼（04 §User）。

**停用與改密碼都必須讓既有 token 立刻失效**，這是本模組最重要的性質：

- 停用的典型情境是「員工今天離職」。若只改 status 欄位，對方手上那張 access
  token 還能用滿 15 分鐘（1A-3 的已知缺口），而那 15 分鐘沒有任何徵兆。
- 改密碼的典型情境是「我懷疑帳號被盜」。只更新雜湊而不撤銷的話，攻擊者的
  session 完全不受影響——使用者以為自己處理完了，實際上什麼都沒發生。

兩者共用 :meth:`~services.identity.auth.AuthService.revoke_all_sessions`。

交易邊界在本層（11 §4.3）：每個公開方法把資料存取包進 `unit_of_work`，而 UoW
必須在 `run_orm` 進 threadpool **之後**才開（ADR-001）——所以每個公開方法都是
同步的，由 api 層負責送進 threadpool。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from django.db import IntegrityError

from common.passwords import hash_password, verify_password
from core import audit
from core.exceptions import ConflictError, InvalidCredentialsError, NotFoundError
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import RoleRepository, UserRepository
from services.identity.auth import AuthService


@dataclass(frozen=True)
class UserView:
    """對外的使用者表示。

    **沒有 password_hash**，而且是刻意用 dataclass 而不是回傳 model：直接把 model
    交給 API 層序列化，只要有人加一個敏感欄位就會自動外流，而那不會有任何測試
    紅燈（除非剛好有人寫了「回應不得包含 password」那條）。
    """

    id: uuid.UUID
    email: str
    display_name: str
    status: str
    roles: tuple[str, ...]


class UserService:
    def __init__(
        self,
        *,
        users: UserRepository | None = None,
        roles: RoleRepository | None = None,
        auth: AuthService | None = None,
    ) -> None:
        self._users = users or UserRepository()
        self._roles = roles or RoleRepository()
        self._auth = auth or AuthService()

    # ── 查詢 ────────────────────────────────────────────────────

    def list_users(self, tenant_id: uuid.UUID) -> list[UserView]:
        with tenant_context(tenant_id), unit_of_work():
            return [self._view(user) for user in self._users.get_queryset().order_by("email")]

    def get_user(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> UserView:
        with tenant_context(tenant_id), unit_of_work():
            return self._view(self._require(user_id))

    # ── 變更 ────────────────────────────────────────────────────

    def create_user(
        self,
        tenant_id: uuid.UUID,
        *,
        email: str,
        display_name: str,
        password: str,
        role: str | None = None,
    ) -> UserView:
        with tenant_context(tenant_id), unit_of_work():
            try:
                user = self._users.create(
                    email=email,
                    display_name=display_name,
                    password_hash=hash_password(password),
                )
            except IntegrityError as exc:
                # UNIQUE(tenant_id, email)。轉成 409 而不是讓它冒成 500——
                # 換一個信箱就能解決的事不該被記成系統故障。
                raise ConflictError("這個 email 在本租戶已存在") from exc

            if role is not None:
                self._roles.assign(user_id=user.id, role_name=role, tenant_id=tenant_id)
            # 建立類的 id 不在 URL 上，稽核 middleware 看不到——少了這行，
            # 紀錄只說得出「有人建了一個使用者」，說不出建了誰（2A-4）。
            audit.describe(resource_id=user.id)
            return self._view(user)

    def update_profile(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        display_name: str | None = None,
    ) -> UserView:
        with tenant_context(tenant_id), unit_of_work():
            user = self._require(user_id)
            before = {"display_name": user.display_name}
            if display_name is not None:
                self._users.set_display_name(user_id, display_name)
                user.display_name = display_name
            # 白名單欄位而不是整個物件：password_hash 與 email 一旦進了稽核，
            # 它會跟著匯出、截圖與工單一起走（10 §5）。
            audit.describe(before=before, after={"display_name": user.display_name})
            return self._view(user)

    def deactivate(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """停用帳號，並讓他手上所有 token 立刻失效。

        順序是先撤銷再改狀態嗎？兩者在同一個請求內完成，順序不影響結果；但
        **兩件事都要做**：只撤銷不改狀態的話他能重新登入，只改狀態不撤銷的話
        現有 session 還能用。
        """
        with tenant_context(tenant_id), unit_of_work():
            user = self._require(user_id)
            # 前值讀實際狀態而不是寫死 "active"：重複停用是真實會發生的操作
            # （前端連點兩下），而稽核上那兩列必須看得出第二次什麼也沒改。
            audit.describe(before={"status": user.status}, after={"status": "inactive"})
            self._users.set_status(user_id, "inactive")

        self._auth.revoke_all_sessions(tenant_id=tenant_id, user_id=user_id)

    def change_password(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        current_password: str,
        new_password: str,
    ) -> None:
        """變更密碼：先驗舊的，再撤銷全部 session。

        **必須驗證舊密碼**——否則任何一張被偷走的 access token 都能直接把帳號
        接管過去（改掉密碼，原主人就再也登不進來）。
        """
        with tenant_context(tenant_id), unit_of_work():
            user = self._require(user_id)
            if not verify_password(current_password, user.password_hash):
                raise InvalidCredentialsError
            self._users.set_password_hash(user_id, hash_password(new_password))

        self._auth.revoke_all_sessions(tenant_id=tenant_id, user_id=user_id)

    # ── 內部 ────────────────────────────────────────────────────

    def _require(self, user_id: uuid.UUID) -> Any:
        """找不到就 404。

        **不是 403**：查詢已經帶了租戶條件，所以「找不到」涵蓋了「屬於別的租戶」。
        回 403 等於承認那個 id 存在，讓人可以拿 id 掃出別的租戶有哪些使用者。
        """
        user = self._users.get_by_id(user_id)
        if user is None:
            raise NotFoundError("找不到該使用者")
        return user

    def _view(self, user: Any) -> UserView:
        return UserView(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            status=user.status,
            roles=self._roles.names_for_user(user.id),
        )

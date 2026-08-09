"""Identity 的資料模型（05 §3.1）。

鐵則 6：model 只有欄位、Meta、``__str__``。業務規則在 Service、查詢在 Repository
——這不是風格問題：長在 model 上的方法通常從 ``self`` 出發直接查關聯資料，
那條路徑完全不經過 Repository 的 tenant filter。

**哪些表有 tenant_id、哪些沒有，決定了哪些表需要 RLS**：

- `Tenant` 自己用 ``id`` 當租戶識別（policy 比對 id 而非 tenant_id）。
- `User` / `Role` / `UserRole` / `RolePermission` 都有 tenant 欄位。其中
  `Role` 與 `RolePermission` 允許 NULL——那代表全租戶共用的系統內建角色。
- `Permission` 是全域字典表（``knowledge:write`` 這類代碼），沒有 tenant、不開
  RLS。理由見 tests/integration/test_rls_identity.py 的
  ``test_permissions_is_intentionally_global``。

`UserRole` 與 `RolePermission` 的 tenant 欄位是**刻意的冗餘**（05 §3.1 的欄位表
只列了兩個外鍵）：少了它，RLS policy 只能 join 回 `Role` / `User` 去推算租戶，
那讓這張表的隔離正確性依賴另一張表的 policy，出事時很難推理。多一欄換 policy
條件單純。

FK 一律不設 CASCADE（05 §5.4）：刪除走顯式的分批清理 worker，避免大量級聯
DELETE 鎖表。這裡用 ``PROTECT`` 讓「忘了先清子表」在開發階段就以錯誤現形。
"""

from __future__ import annotations

import uuid

from django.db import models


class TimestampedModel(models.Model):
    """標配三欄（05 §3 前言）：建立、更新、軟刪除時間。

    `deleted_at` 在 identity 這幾張表目前沒有使用者可觸發的軟刪除流程
    （05 §5.4 的軟刪除只涵蓋 KB / document / conversation / prompt），
    但欄位先留著，讓所有表的形狀一致——事後加欄位到大表要走三步走遷移。
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class Tenant(TimestampedModel):
    """租戶本體。跨租戶讀得到這張表 = 客戶名單外流，因此它自己也受 RLS 管。"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.TextField()
    slug = models.TextField(unique=True)
    plan = models.TextField(default="free")
    status = models.TextField(default="active")
    settings = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "identity_tenant"

    def __str__(self) -> str:
        return f"Tenant({self.slug})"


class TenantDirectory(models.Model):
    """登入用的 slug → tenant_id 對照表。**刻意不開 RLS**（1A-3）。

    **為什麼需要它**：登入發生在拿到 token 之前，那時還沒有租戶身分；而
    `identity_tenant` 與 `identity_user` 都有 RLS，沒有 ``app.tenant_id`` 就一筆
    都查不到。於是「先查出這個人屬於哪個租戶」在 RLS 之下是不可能的——死結。

    **為什麼這樣安全**：本表只有 slug、tenant_id、status 三個值。slug 本來就是
    公開資訊（出現在網址與物件儲存路徑），完全不含客戶資料或 PII。相較之下，
    另外兩條路——把 email 對照表攤開、或建一個 BYPASSRLS 角色——暴露的都是
    整個平台的使用者資料。

    **內容由 trigger 維護**（見 migration 0004）而非應用程式：應用程式維護的話，
    任何一條沒走 TenantService 的租戶建立（測試 factory、data migration、日後的
    匯入腳本）都會漏掉一列，症狀是「這個租戶就是登不進去」，而查詢租戶表時
    資料明明都在。

    未來改成子網域登入（``acme.lumina.app``）時，這張表原封不動沿用，只是 slug
    的來源從請求 body 換成 Host 標頭。
    """

    tenant_id = models.UUIDField(primary_key=True)
    slug = models.TextField(unique=True)
    status = models.TextField()

    class Meta:
        db_table = "identity_tenant_directory"
        # 名稱刻意帶 directory：讓「這張表沒有租戶隔離」在每個查詢裡都看得見。

    def __str__(self) -> str:
        return f"TenantDirectory({self.slug})"


class User(TimestampedModel):
    """租戶內的使用者。密碼雜湊為 argon2id（10 §）；本層只存欄位。"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="users")
    email = models.TextField()
    password_hash = models.TextField()
    display_name = models.TextField(default="")
    status = models.TextField(default="active")
    email_verified_at = models.DateTimeField(null=True, blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    preferences = models.JSONField(default=dict, blank=True)
    # 10 §2.1 的全域撤銷開關：改密碼或停用帳號時 +1，該使用者手上所有尚未過期的
    # token 一次失效。這是唯一不需要「知道有哪些 session 存在」的撤銷手段——
    # 帳號被盜時，你根本不知道攻擊者建立了幾個。
    token_version = models.IntegerField(default=1)

    class Meta:
        db_table = "identity_user"
        constraints = [
            # 租戶**內**唯一，不是全域唯一：同一個人在兩家客戶各有帳號是正常情境
            # （顧問、外包、集團調動）。做成全域唯一時，第二家客戶的建立流程會以
            # 「email 已被使用」失敗，而訊息不會提到是別的租戶佔用了它。
            models.UniqueConstraint(fields=["tenant", "email"], name="uq_user_tenant_email"),
        ]
        indexes = [
            # 05 §4 指名的查詢：登入與使用者列表。首欄必須是 tenant——首欄不是它的
            # 複合索引，在「先過濾租戶再排序」的查詢裡幾乎用不到。
            models.Index(fields=["tenant", "status"], name="ix_user_tenant_status"),
        ]

    def __str__(self) -> str:
        return f"User({self.email})"


class Role(TimestampedModel):
    """角色。``tenant`` 為 NULL = 系統內建角色（Owner/Admin/Editor/Viewer），全租戶共用。

    這是整個 identity 唯一允許 NULL 租戶的地方，也因此是 RLS policy 的例外：
    policy 必須寫成 ``tenant_id IS NULL OR tenant_id = 當前租戶``。少了前半，
    NULL 與任何值比較的結果都是 NULL 而非 true，四個系統角色會對所有人隱形。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.PROTECT, related_name="roles", null=True, blank=True
    )
    name = models.TextField()
    is_system = models.BooleanField(default=False)

    class Meta:
        db_table = "identity_role"
        constraints = [
            # nulls_distinct=False：預設語意下兩個 NULL 互不相等，於是可以建出
            # 無限多個叫 "owner" 的系統角色。系統角色的唯一性正是「不可修改的
            # 內建角色」這個保證的基礎，所以這裡顯式關掉。
            models.UniqueConstraint(
                fields=["tenant", "name"],
                name="uq_role_tenant_name",
                nulls_distinct=False,
            ),
        ]

    def __str__(self) -> str:
        scope = "system" if self.tenant_id is None else str(self.tenant_id)
        return f"Role({self.name}@{scope})"


class Permission(TimestampedModel):
    """全域權限字典：``resource:action``（例如 ``knowledge:write``）。

    沒有 tenant、不開 RLS：內容對所有租戶完全相同，也不含客戶資料。給它
    tenant_id 的話每個租戶要存一份一模一樣的複本，而「兩個租戶的
    knowledge:write 是不是同一件事」會變成要 join 才能回答的問題。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.TextField(unique=True)
    description = models.TextField(default="", blank=True)

    class Meta:
        db_table = "identity_permission"

    def __str__(self) -> str:
        return f"Permission({self.code})"


class UserRole(models.Model):
    """使用者 ↔ 角色。複合主鍵（05 §3.1），tenant 為 RLS 用的冗餘欄。"""

    pk = models.CompositePrimaryKey("user", "role")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="user_roles")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="user_roles")
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="user_roles")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "identity_user_role"
        indexes = [
            models.Index(fields=["tenant", "user"], name="ix_user_role_tenant_user"),
        ]

    def __str__(self) -> str:
        return f"UserRole({self.user_id}→{self.role_id})"


class RolePermission(models.Model):
    """角色 ↔ 權限。``tenant`` 跟著 role 走：系統角色的授權列為 NULL（全租戶可見）。"""

    pk = models.CompositePrimaryKey("role", "permission")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="role_permissions")
    permission = models.ForeignKey(
        Permission, on_delete=models.PROTECT, related_name="role_permissions"
    )
    tenant = models.ForeignKey(
        Tenant, on_delete=models.PROTECT, related_name="role_permissions", null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "identity_role_permission"
        indexes = [
            models.Index(fields=["tenant", "role"], name="ix_role_perm_tenant_role"),
        ]

    def __str__(self) -> str:
        return f"RolePermission({self.role_id}→{self.permission_id})"


class ResourceGrant(TimestampedModel):
    """資源級授權：某個 user/role 對某個 KB/tool/prompt 的 read/write/admin。

    **本階段只建表，不接判定邏輯**（13 §4：自訂角色與資源級 grant 的判定延後至
    Phase 5；`PermissionService` 的判定順序見 10 §）。先建的理由是它是租戶資料表
    ——RLS policy 與索引在建表當下一起做的成本，遠低於日後對一張有資料的表補上，
    而「先上線再補隔離」正是本專案明文要避免的（13 §4 的裁切原則）。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="resource_grants")
    principal_type = models.TextField()  # user / role
    principal_id = models.UUIDField()
    resource_type = models.TextField()  # knowledge_base / tool / prompt
    resource_id = models.UUIDField()
    access = models.TextField()  # read / write / admin

    class Meta:
        db_table = "identity_resource_grant"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "principal_type", "principal_id", "resource_type", "resource_id"],
                name="uq_resource_grant",
            ),
        ]
        indexes = [
            # 判定時的查詢形狀：先租戶、再資源，最後看有哪些 principal 被授權。
            models.Index(
                fields=["tenant", "resource_type", "resource_id"],
                name="ix_resource_grant_resource",
            ),
        ]

    def __str__(self) -> str:
        return f"ResourceGrant({self.principal_type}:{self.principal_id}→{self.resource_type})"

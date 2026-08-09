"""權限模型（10 §3）。

判定順序（10 §3）：租戶符合 → 具備 permission code → 資源級 grant（若該資源啟用
ACL）→ 放行。**本階段只做到第二步**：資源級 grant 的判定依 13 §4 延後到 Phase 5，
表已經建好（`ResourceGrant`）但沒有人讀它。

**權限碼只種「現在真的有端點在用」的**（1A-4 決定 A）。種了卻沒有端點的碼無法
驗證對錯——名字、粒度、屬於哪個角色全是猜的——而且它會出現在權限管理介面上，
讓人以為那個功能已經存在。其餘的碼隨各自的工作包進來（09 §2.3 起）。

**這份常數與 DB 的資料是兩份**，必然會漂，而漂掉時完全沒有症狀：程式照跑，
只是某個角色實際擁有的權限跟你以為的不一樣。對帳由
`tests/integration/test_permission_seed.py` 負責——它查 DB 實際的列再跟這裡比。
"""

from __future__ import annotations

from enum import StrEnum


class SystemRole(StrEnum):
    """四個內建角色（10 §3）。不可修改、所有租戶共用（``tenant_id`` 為 NULL）。"""

    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


# 目前有端點在用的權限碼。格式 ``resource:action``（10 §3）。
PERMISSION_CODES: frozenset[str] = frozenset(
    {
        "user:read",  # GET /users、GET /users/{id}
        "user:write",  # POST /users、PATCH /users/{id}、deactivate
        "tenant:read",  # GET /tenants/current
        "tenant:admin",  # PATCH /tenants/current
    }
)

# 角色 → 權限。這是**產品決策**的程式化版本，改動需要 review。
#
# 兩條刻意的界線：
#   - `tenant:admin` 只給 Owner。Admin 的定位是「管人」而不是「管公司」，
#     混在一起的話一個被入侵的 Admin 帳號可以改掉整個租戶的設定。
#   - `user:write` 只給 Owner / Admin。帳號管理權等於可以自己造一個更高權限的
#     帳號，所以它是這張表裡最敏感的一格。
#
# Editor 與 Viewer 目前看起來一樣，因為它們的差別在知識庫（`knowledge:*`），
# 那是 1B 的範圍。
SYSTEM_ROLE_PERMISSIONS: dict[SystemRole, frozenset[str]] = {
    SystemRole.OWNER: frozenset({"user:read", "user:write", "tenant:read", "tenant:admin"}),
    SystemRole.ADMIN: frozenset({"user:read", "user:write", "tenant:read"}),
    SystemRole.EDITOR: frozenset({"tenant:read"}),
    SystemRole.VIEWER: frozenset({"tenant:read"}),
}


class PermissionService:
    """權限判定的唯一入口（10 §3）。

    **輸入是 token 裡的角色名稱，不是資料庫查詢**：`roles` 已經在登入時寫進
    access token 的 claim，每個請求再查一次 DB 等於把一次查詢乘上全部流量。
    代價是角色變更要等 token 換發才生效——而「立刻生效」的需求（停用帳號、
    改密碼）走的是 `token_version` 全域撤銷，不靠這條路徑。

    自訂角色（13 §4 延後至 Phase 5）進來時，這裡要改成「系統角色查常數、
    自訂角色查 DB（帶快取）」，介面不變。
    """

    def permissions_for(self, roles: tuple[str, ...]) -> frozenset[str]:
        granted: set[str] = set()
        for role in roles:
            try:
                granted |= SYSTEM_ROLE_PERMISSIONS[SystemRole(role)]
            except ValueError:
                # 不認得的角色名稱**不貢獻任何權限**，也不報錯：Phase 5 的自訂
                # 角色會走這條路，而在那之前，未知角色的正確語意就是「沒有權限」。
                continue
        return frozenset(granted)

    def has(self, roles: tuple[str, ...], code: str) -> bool:
        return code in self.permissions_for(roles)

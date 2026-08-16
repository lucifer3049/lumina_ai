"""權限模型（10 §3）。

判定順序（10 §3）：租戶符合 → 具備 permission code → 資源級 grant（若該資源啟用
ACL）→ 放行。**本階段只做到第二步**：資源級 grant 的判定依 13 §4 延後到 Phase 5，
表已經建好（`ResourceGrant`）但沒有人讀它。

**權限碼只種「現在真的有端點在用」的**（1A-4 決定 A）。種了卻沒有端點的碼無法
驗證對錯——名字、粒度、屬於哪個角色全是猜的——而且它會出現在權限管理介面上，
讓人以為那個功能已經存在。其餘的碼隨各自的工作包進來（09 §2.3 起）。

**碼由各 bounded context 自己宣告，本模組只負責匯總**（1B-2 改）。1A 時全部寫在
這裡，每加一個 context 就要回頭改 identity——而 ADR-006 的模組邊界說「新增來源 =
新增一個模組」。匯總點留在 identity 的理由是 :class:`PermissionService` 與
``identity_permission`` 表都在這裡；貢獻者只需要提供兩個常數（見
`services/knowledge/permissions.py` 的形狀）。

依賴方向是 **identity → 各 context**，反向禁止：knowledge 若 import 本模組取
``SystemRole`` 就是循環 import。因此貢獻者以**字串**當角色鍵，由本模組轉回列舉。

**這份常數與 DB 的資料是兩份**，必然會漂，而漂掉時完全沒有症狀：程式照跑，
只是某個角色實際擁有的權限跟你以為的不一樣。對帳由
`tests/integration/test_permission_seed.py` 負責——它查 DB 實際的列再跟這裡比。
"""

from __future__ import annotations

from enum import StrEnum

from services.conversation.permissions import CHAT_PERMISSIONS, CHAT_ROLE_PERMISSIONS
from services.knowledge.permissions import KNOWLEDGE_PERMISSIONS, KNOWLEDGE_ROLE_PERMISSIONS
from services.rag.permissions import RAG_PERMISSIONS, RAG_ROLE_PERMISSIONS


class SystemRole(StrEnum):
    """四個內建角色（10 §3）。不可修改、所有租戶共用（``tenant_id`` 為 NULL）。"""

    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


# identity 自己的碼。格式 ``resource:action``（10 §3）。
IDENTITY_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("user:read", "檢視租戶內的使用者"),  # GET /users、GET /users/{id}
    ("user:write", "建立、更新、停用使用者"),  # POST /users、PATCH /users/{id}、deactivate
    ("tenant:read", "檢視本租戶資訊"),  # GET /tenants/current
    ("tenant:admin", "變更本租戶設定"),  # PATCH /tenants/current
)

# 各 context 的貢獻。**新增 context 時只加進這個 tuple**，下面的匯總不必動。
_CONTRIBUTIONS: tuple[tuple[tuple[tuple[str, str], ...], dict[str, frozenset[str]]], ...] = (
    (IDENTITY_PERMISSIONS, {}),  # identity 的角色綁定在下方直接列出，見 _IDENTITY_ROLES
    (KNOWLEDGE_PERMISSIONS, KNOWLEDGE_ROLE_PERMISSIONS),
    (RAG_PERMISSIONS, RAG_ROLE_PERMISSIONS),
    (CHAT_PERMISSIONS, CHAT_ROLE_PERMISSIONS),
)

# 全系統的權限碼字典（= 各 context 的聯集）。
PERMISSION_CODES: frozenset[str] = frozenset(
    code for permissions, _ in _CONTRIBUTIONS for code, _ in permissions
)

# 權限碼 → 描述，供 seed migration 與權限管理介面使用。
PERMISSION_DESCRIPTIONS: dict[str, str] = {
    code: description for permissions, _ in _CONTRIBUTIONS for code, description in permissions
}

# identity 自己的角色 → 權限。這是**產品決策**的程式化版本，改動需要 review。
#
# 兩條刻意的界線：
#   - `tenant:admin` 只給 Owner。Admin 的定位是「管人」而不是「管公司」，
#     混在一起的話一個被入侵的 Admin 帳號可以改掉整個租戶的設定。
#   - `user:write` 只給 Owner / Admin。帳號管理權等於可以自己造一個更高權限的
#     帳號，所以它是這張表裡最敏感的一格。
_IDENTITY_ROLES: dict[str, frozenset[str]] = {
    "owner": frozenset({"user:read", "user:write", "tenant:read", "tenant:admin"}),
    "admin": frozenset({"user:read", "user:write", "tenant:read"}),
    "editor": frozenset({"tenant:read"}),
    "viewer": frozenset({"tenant:read"}),
}


def _aggregate_roles() -> dict[SystemRole, frozenset[str]]:
    """把各 context 的角色綁定併成一張表。

    未出現在某個 context 的角色**不是錯誤**——那代表該角色在那個領域沒有任何權限
    （例如 Viewer 之於未來的 billing）。因此以四個系統角色為主鍵展開，而不是以
    貢獻者的鍵集合為準：後者會讓「某個 context 漏列一個角色」變成那個角色整格消失。
    """
    contributions = [_IDENTITY_ROLES, *(roles for _, roles in _CONTRIBUTIONS if roles)]
    return {
        role: frozenset().union(*(source.get(role.value, frozenset()) for source in contributions))
        for role in SystemRole
    }


# 全系統的角色 → 權限（= 各 context 的聯集）。
SYSTEM_ROLE_PERMISSIONS: dict[SystemRole, frozenset[str]] = _aggregate_roles()


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

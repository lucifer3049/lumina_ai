"""驗收：系統角色 ↔ 權限碼的對應表本身（10 §3）。

**這張表是產品決策的程式化版本**，所以它的正確性不能只靠「跑起來沒事」——
權限給多了不會有任何錯誤訊息，只有稽核或事故時才會發現。

本檔驗的是**表的內在一致性**（不需要資料庫）：

1. 用到的碼都在字典裡——打錯字的權限碼會變成「一個永遠沒人有的權限」，
   而掛著它的端點對所有人都是 403，症狀看起來像權限設定壞掉。
2. 四個角色是**單調包含**的：Owner ⊇ Admin ⊇ Editor ⊇ Viewer。這是使用者對
   「角色」的直覺（層級），破壞它會產生「Admin 能做但 Owner 不能」這種沒有人
   預期得到的組合。
3. 只種**現在有端點的碼**（1A-4 決定）。種了卻沒有端點的權限碼無法驗證對錯，
   還會出現在管理介面上讓人以為功能已經存在。

DB 裡的資料是否與本表一致，由 `tests/integration/test_permission_seed.py` 驗
——那是另一種失敗（migration 寫錯或漏跑），需要真的連資料庫才看得出來。
"""

from __future__ import annotations

import itertools

from services.identity.permissions import (
    PERMISSION_CODES,
    SYSTEM_ROLE_PERMISSIONS,
    SystemRole,
)

# 目前有端點的資源：users / tenant（1A-4）、knowledge（1B-2）、rag（1C-4）。
# chat:* / tool:* 等隨各自的工作包進來（09 §2.3 起）。
#
# 這份清單刻意手寫而不是從各 context 的宣告推導：推導的話它就變成「重述程式碼」，
# 對「多種了一個沒有端點的碼」永遠是綠的——而那正是本檔要擋的事。
EXPECTED_CODES_IN_SCOPE = {
    "user:read",
    "user:write",
    "tenant:read",
    "tenant:admin",
    "knowledge:read",
    "knowledge:write",
    "knowledge:admin",
    "rag:query",
}

# 由寬到窄。單調包含的順序也就是這個。
ROLES_WIDEST_FIRST = (
    SystemRole.OWNER,
    SystemRole.ADMIN,
    SystemRole.EDITOR,
    SystemRole.VIEWER,
)


def test_dictionary_only_contains_codes_that_have_endpoints() -> None:
    """字典 = 目前真的有端點在用的碼（1A-4 決定 A）。"""
    assert PERMISSION_CODES == EXPECTED_CODES_IN_SCOPE, (
        "權限字典與本階段的端點範圍不符——新增碼請與端點同一個工作包進來，"
        "否則那個碼無法被驗證，卻會出現在權限管理介面上"
    )


def test_every_role_uses_only_known_codes() -> None:
    """角色引用的碼都必須存在於字典。

    打錯字的碼（``user:wirte``）不會報錯，它只是變成一個沒有人擁有的權限——
    掛著它的端點對所有人回 403，看起來像權限設定壞掉而不是拼字錯誤。
    """
    unknown = {
        role: sorted(codes - PERMISSION_CODES)
        for role, codes in SYSTEM_ROLE_PERMISSIONS.items()
        if codes - PERMISSION_CODES
    }

    assert not unknown, f"角色引用了字典裡沒有的權限碼：{unknown}"


def test_all_four_system_roles_are_defined() -> None:
    """四個系統角色都要有對應（10 §3：Owner/Admin/Editor/Viewer 不可修改）。"""
    assert set(SYSTEM_ROLE_PERMISSIONS) == set(SystemRole)


def test_roles_are_monotonically_inclusive() -> None:
    """Owner ⊇ Admin ⊇ Editor ⊇ Viewer。

    使用者對角色的直覺是「層級」——升一級不該失去任何能力。破壞單調性會產生
    「Admin 做得到但 Owner 做不到」這種沒有人預期得到的組合，而它不會有錯誤，
    只會變成一張很難解釋的支援單。
    """
    for wider, narrower in itertools.pairwise(ROLES_WIDEST_FIRST):
        missing = SYSTEM_ROLE_PERMISSIONS[narrower] - SYSTEM_ROLE_PERMISSIONS[wider]
        assert not missing, f"{narrower} 有 {sorted(missing)}，但更高階的 {wider} 沒有"


def test_only_owner_can_administer_the_tenant() -> None:
    """``tenant:admin`` 只屬於 Owner（1A-4 決定 ②）。

    它涵蓋的是公司層級設定的變更。Admin 的定位是「管人」，不是「管公司」——
    兩者混在一起的話，一個被入侵的 Admin 帳號可以改掉整個租戶的設定。
    """
    holders = {role for role, codes in SYSTEM_ROLE_PERMISSIONS.items() if "tenant:admin" in codes}

    assert holders == {SystemRole.OWNER}


def test_write_permissions_are_limited_to_owner_and_admin() -> None:
    """``user:write`` 只有 Owner / Admin 有。

    這條與上一條成對：把「誰能建立與停用帳號」釘死。少了它，日後調整 Editor 的
    權限時很容易順手加上——而帳號管理權等於可以自己造一個更高權限的帳號。
    """
    holders = {role for role, codes in SYSTEM_ROLE_PERMISSIONS.items() if "user:write" in codes}

    assert holders == {SystemRole.OWNER, SystemRole.ADMIN}

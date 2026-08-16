"""驗收：權限碼由各 context 自己宣告，再匯總成單一字典（1B-2 決定）。

**為什麼要拆**：1A 的權限碼全部寫在 `services/identity/permissions.py` 的兩個常數
裡。那在只有 identity 的時候沒問題，但每加一個 bounded context（knowledge、chat、
tool…）就要回頭改 identity——ADR-006 的模組邊界說「新增來源 = 新增一個模組」，而
權限是模組定義的一部分。更實際的理由是 review：`knowledge:write` 該給哪些角色，
應該和知識庫的端點一起被看到，不是散到另一個 context 的檔案裡。

**匯總點仍在 identity**（`PermissionService` 在那裡，且 `identity_permission` 是它的
表），所以本檔驗的是兩件事：

1. 每個 context 的宣告模組長得一樣（同一個介面），因此加 context 不需要改匯總邏輯。
2. 匯總結果 = 各 context 的聯集，**且沒有任何一個 context 的碼在匯總時掉了**。

第 2 點是這個設計唯一的新風險：拆開之後，「宣告了但沒被匯總」會讓那個 context 的
端點對所有人回 403，而權限字典看起來完全正常。
"""

from __future__ import annotations

from services.identity.permissions import PERMISSION_CODES, SYSTEM_ROLE_PERMISSIONS, SystemRole
from services.knowledge.permissions import KNOWLEDGE_PERMISSIONS, KNOWLEDGE_ROLE_PERMISSIONS
from services.rag.permissions import RAG_PERMISSIONS, RAG_ROLE_PERMISSIONS

# 1B-2 進來的三個碼（09 §2.3）。
EXPECTED_KNOWLEDGE_CODES = {"knowledge:read", "knowledge:write", "knowledge:admin"}

# 1C-4 進來的碼（09 §2.3 的 `POST /rag/query`）。
EXPECTED_RAG_CODES = {"rag:query"}


def test_knowledge_declares_its_own_codes() -> None:
    """knowledge 的碼宣告在 knowledge 自己的模組裡。"""
    assert {code for code, _ in KNOWLEDGE_PERMISSIONS} == EXPECTED_KNOWLEDGE_CODES


def test_every_knowledge_code_has_a_description() -> None:
    """描述會出現在權限管理介面上；空字串等於讓管理員猜這個碼是做什麼的。"""
    missing = [code for code, description in KNOWLEDGE_PERMISSIONS if not description.strip()]

    assert not missing, f"以下權限碼沒有描述：{missing}"


def test_aggregated_dictionary_contains_every_context() -> None:
    """匯總結果必須含 knowledge 的碼。

    **這是拆分之後唯一的新失效模式**：宣告了卻沒被匯總進 `PERMISSION_CODES`，
    於是端點宣告的 scope 永遠不會被任何角色滿足——所有人都拿到 403，而權限管理
    介面上看起來一切正常（那裡讀的是 DB，DB 的種子也來自匯總結果，兩邊一起錯）。
    """
    assert EXPECTED_KNOWLEDGE_CODES <= PERMISSION_CODES, (
        f"匯總後缺少 knowledge 的權限碼：{sorted(EXPECTED_KNOWLEDGE_CODES - PERMISSION_CODES)}"
    )


def test_role_bindings_are_aggregated_too() -> None:
    """角色 → 權限的綁定同樣要匯總，而且**逐角色**檢查。

    只驗「碼在字典裡」是不夠的：碼進了字典但沒綁到任何角色，結果與沒匯總完全相同
    （所有人 403）。而且錯法通常是漏掉其中一個角色，那更難發現——大部分測試帳號
    是 owner，權限齊全，紅燈只會出現在沒有人測的那個角色上。
    """
    for role_name, codes in KNOWLEDGE_ROLE_PERMISSIONS.items():
        # 貢獻者用字串當鍵（避免循環 import，見 services/knowledge/permissions.py），
        # 匯總後的表以 SystemRole 為鍵——轉一次順帶驗證角色名稱拼對了：打錯的話
        # SystemRole(...) 直接 ValueError，而不是安靜地比對一個不存在的角色。
        aggregated = SYSTEM_ROLE_PERMISSIONS[SystemRole(role_name)]
        assert codes <= aggregated, (
            f"{role_name} 的 knowledge 權限沒有進匯總：{sorted(codes - aggregated)}"
        )


def test_knowledge_role_matrix_follows_the_product_decision() -> None:
    """角色 × knowledge 權限（2026-08-09 產品決策）。

    - ``knowledge:read`` 給全部四個角色——Viewer 的定位是「能查、不能改」，看不到
      文件清單的話連「這個答案引用的是哪份文件」都無從確認。
    - ``knowledge:write`` 不給 Viewer——上傳與刪除文件會改變所有人的答案來源。
    - ``knowledge:admin`` 只給 Owner / Admin——改 chunk 策略或 reindex 會讓整個 KB
      重算，那是維運等級的動作，不該落在日常編輯者手上。
    """
    expected = {
        SystemRole.OWNER: {"knowledge:read", "knowledge:write", "knowledge:admin"},
        SystemRole.ADMIN: {"knowledge:read", "knowledge:write", "knowledge:admin"},
        SystemRole.EDITOR: {"knowledge:read", "knowledge:write"},
        SystemRole.VIEWER: {"knowledge:read"},
    }

    actual = {role: set(codes) for role, codes in KNOWLEDGE_ROLE_PERMISSIONS.items()}

    assert actual == expected, f"knowledge 的角色矩陣與產品決策不符：{actual}"


def test_editor_and_viewer_are_finally_different() -> None:
    """Editor 與 Viewer 在 1A 之後第一次有實質差別。

    `services/identity/permissions.py` 原本註明「兩者目前看起來一樣，差別在
    ``knowledge:*``，那是 1B 的範圍」。這條測試把那句註解變成斷言——若哪天有人把
    ``knowledge:write`` 也給了 Viewer，四個角色會退化成三個，而不會有任何紅燈。
    """
    editor = SYSTEM_ROLE_PERMISSIONS[SystemRole.EDITOR]
    viewer = SYSTEM_ROLE_PERMISSIONS[SystemRole.VIEWER]

    assert editor != viewer, "Editor 與 Viewer 的權限完全相同——角色設計失去意義"
    assert viewer < editor, "Viewer 應該是 Editor 的子集"


# ── rag（1C-4）─────────────────────────────────────────────────


def test_rag_declares_its_own_codes() -> None:
    """rag 是第三個 bounded context，形狀必須與前兩個相同。

    形狀一致才是「加 context 不必改匯總邏輯」這個設計的兌現條件——不一致的話，
    匯總那段會長出一個 if，而下一個 context 會再長一個。
    """
    assert {code for code, _ in RAG_PERMISSIONS} == EXPECTED_RAG_CODES


def test_every_rag_code_has_a_description() -> None:
    missing = [code for code, description in RAG_PERMISSIONS if not description.strip()]

    assert not missing, f"以下權限碼沒有描述：{missing}"


def test_rag_codes_are_aggregated() -> None:
    """漏了匯總的症狀：所有人打 `/rag/query` 都拿到 403，而權限管理介面完全正常。"""
    assert EXPECTED_RAG_CODES <= PERMISSION_CODES, (
        f"匯總後缺少 rag 的權限碼：{sorted(EXPECTED_RAG_CODES - PERMISSION_CODES)}"
    )


def test_rag_role_bindings_are_aggregated() -> None:
    for role_name, codes in RAG_ROLE_PERMISSIONS.items():
        aggregated = SYSTEM_ROLE_PERMISSIONS[SystemRole(role_name)]
        assert codes <= aggregated, (
            f"{role_name} 的 rag 權限沒有進匯總：{sorted(codes - aggregated)}"
        )


def test_rag_query_is_granted_to_every_role() -> None:
    """`rag:query` 四個角色都有（2026-08-16 產品決策）。

    問問題就是這個產品本身——Viewer 查不了的話，「能查、不能改」這個定位不成立。
    它讀得到的內容完全等同 `knowledge:read` 已經給的，因此不構成新的暴露面。

    **那為什麼要獨立成一個碼**：檢索每一次都要花錢（embedding，2B 之後還有 rerank），
    而「能看文件清單」與「能無限次觸發付費呼叫」是兩件事——2A 的 quota 需要一個掛得
    上去的碼。今天四個角色都給，不代表明天不會有人想關掉其中一個。
    """
    expected = {
        SystemRole.OWNER: {"rag:query"},
        SystemRole.ADMIN: {"rag:query"},
        SystemRole.EDITOR: {"rag:query"},
        SystemRole.VIEWER: {"rag:query"},
    }

    actual = {role: set(codes) for role, codes in RAG_ROLE_PERMISSIONS.items()}

    assert actual == expected, f"rag 的角色矩陣與產品決策不符：{actual}"

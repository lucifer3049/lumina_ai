"""Knowledge 的權限碼宣告（10 §3、09 §2.3）。

**每個 bounded context 宣告自己的權限碼**（1B-2 決定）。1A 時全部寫在
`services/identity/permissions.py`，那在只有 identity 的時候沒問題；但每加一個
context 就要回頭改 identity，而 ADR-006 的模組邊界說「新增來源 = 新增一個模組」。
更實際的理由是 review：`knowledge:write` 該給哪些角色，應該和知識庫的端點一起被
看到，而不是散在另一個 context 的檔案裡。

匯總仍在 identity（`PermissionService` 與 `identity_permission` 表都在那裡），
本模組只負責「宣告」。拆開之後多了一個失效模式——宣告了卻沒被匯總，症狀是所有人
對這些端點都拿到 403 而權限管理介面完全正常——由
`tests/unit/test_permission_registry.py` 兩條測試釘住。

**只宣告現在真的有端點在用的碼**（沿用 1A-4 的決定）：種了卻沒有端點的碼無法驗證
對錯，而且會出現在權限管理介面上，讓人以為那個功能已經存在。

**角色用字串而不是 import identity 的 ``SystemRole``**：匯總的方向是 identity →
knowledge，反過來再 import 就是循環（實際會 ImportError）。而且 ADR-006 的 bounded
context 之間本來就不該互相依賴——knowledge 只需要知道「有哪些系統角色名稱」，那是
一個字串契約。型別安全由 `tests/unit/test_permission_registry.py` 補回來：它以
``SystemRole`` 為鍵比對本模組的宣告，角色名稱打錯會紅（``SystemRole`` 是 StrEnum，
與字串鍵可直接比對）。
"""

from __future__ import annotations

# (code, 描述)。描述會出現在權限管理介面上，因此要寫給管理員看，不是給工程師看。
KNOWLEDGE_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("knowledge:read", "檢視知識庫與其中的文件"),
    ("knowledge:write", "上傳與刪除文件"),
    ("knowledge:admin", "建立與刪除知識庫、變更切塊與檢索設定"),
)

# 角色 → 權限（2026-08-09 產品決策）。這是**產品決策**的程式化版本，改動需要 review。
#
# 三條界線：
#   - `knowledge:read` 給全部四個角色。Viewer 的定位是「能查、不能改」——看不到文件
#     清單的話，連「這個答案引用的是哪份文件」都無從確認。
#   - `knowledge:write` 不給 Viewer：上傳與刪除文件會改變**所有人**的答案來源。
#   - `knowledge:admin` 只給 Owner / Admin：改 chunk 策略或刪整個 KB 會讓知識庫需要
#     重算或整批失效，那是維運等級的動作，不該落在日常編輯者手上。
#
# 這也是 Editor 與 Viewer 在 1A 之後第一次有實質差別（identity 的 permissions.py
# 原本就註明「兩者的差別在 knowledge:*，那是 1B 的範圍」）。
KNOWLEDGE_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset({"knowledge:read", "knowledge:write", "knowledge:admin"}),
    "admin": frozenset({"knowledge:read", "knowledge:write", "knowledge:admin"}),
    "editor": frozenset({"knowledge:read", "knowledge:write"}),
    "viewer": frozenset({"knowledge:read"}),
}

"""RAG 的權限碼宣告（10 §3、09 §2.3）。

形狀與 `services/knowledge/permissions.py` 完全相同——**那正是重點**：加一個 bounded
context 只要新增一個這樣的模組，再把它掛進 `services/identity/permissions.py` 的
`_CONTRIBUTIONS`，匯總邏輯一行都不必動。形狀漂掉的話，匯總那段會長出一個 if，而下一個
context 會再長一個。
"""

from __future__ import annotations

# (code, 描述)。描述會出現在權限管理介面上，因此要寫給管理員看，不是給工程師看。
RAG_PERMISSIONS: tuple[tuple[str, str], ...] = (("rag:query", "對知識庫發問與檢索"),)

# 角色 → 權限（2026-08-16 產品決策）。
#
# **四個角色都給。** 問問題就是這個產品本身——Viewer 的定位是「能查、不能改」，查不了
# 的話那個角色沒有任何意義。它經檢索讀得到的內容完全等同 `knowledge:read` 已經給的
# （同一批文件的內容），所以不構成新的暴露面。
#
# **那為什麼要獨立成一個碼，而不是沿用 `knowledge:read`**：檢索每一次都要花錢
# （embedding 呼叫，2B 之後還有 rerank），而「能看文件清單」與「能無限次觸發付費呼叫」
# 是兩件不同的事。2A 的 quota 與用量控管需要一個掛得上去的碼——今天四個角色都給，
# 不代表明天不會有人想把某個角色關掉。
RAG_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset({"rag:query"}),
    "admin": frozenset({"rag:query"}),
    "editor": frozenset({"rag:query"}),
    "viewer": frozenset({"rag:query"}),
}

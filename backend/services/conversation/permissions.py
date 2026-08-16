"""Chat 的權限碼宣告（10 §3、09 §2.4）。

形狀同前三個 context（identity / knowledge / rag）。

**只有一個碼，而且它只管一半的事。** `chat:use` 是**角色權限**：這個人能不能用聊天。
對話的詳情／修改／刪除走的是**擁有者判定**（09 §2.4 的「擁有者」），那是**資源權限**，
與角色完全無關——Owner 也讀不到別人的對話。

兩者混為一談的後果很具體：只檢查 `chat:use` 的話，同租戶的任何人都讀得到別人的對話。
而 **RLS 擋不住這件事**——它是租戶級的隔離，同一個租戶裡的兩個使用者在 policy 眼中
一模一樣。擁有者判定沒有第二道防線，程式漏了就是漏了。
"""

from __future__ import annotations

# (code, 描述)。描述會出現在權限管理介面上，因此要寫給管理員看。
CHAT_PERMISSIONS: tuple[tuple[str, str], ...] = (("chat:use", "使用對話問答"),)

# 角色 → 權限（2026-08-16 產品決策）。
#
# **四個角色都給**：問答就是這個產品本身，Viewer 用不了的話那個角色沒有意義
# （同 `rag:query` 的理由）。它讀得到的內容不會超出 `knowledge:read` 已經給的範圍。
CHAT_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset({"chat:use"}),
    "admin": frozenset({"chat:use"}),
    "editor": frozenset({"chat:use"}),
    "viewer": frozenset({"chat:use"}),
}

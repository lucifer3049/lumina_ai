"""Platform context 的權限碼（04 §8.2／§8.3，2A-3／2A-4）。

`analytics:read` 只給 owner／admin：用量輪廓是管理面資訊——一般成員看得到自己
的對話，看不到全租戶的消費統計。界線的劃法同 `tenant:admin` 的「管公司」原則，
但鬆一級：admin 看報表合理，改公司設定（tenant:admin）仍只有 owner。
"""

from __future__ import annotations

ANALYTICS_PERMISSIONS: tuple[tuple[str, str], ...] = (("analytics:read", "查看用量與成本報表"),)

ANALYTICS_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset({"analytics:read"}),
    "admin": frozenset({"analytics:read"}),
}

# 稽核（2A-4）。界線同 analytics：admin 本來就在管使用者（建立、停用），看「誰做了
# 什麼」是同一份職務；editor／viewer 看不到——稽核含 IP 與他人的操作軌跡，是比
# 用量輪廓更敏感的一份東西。
AUDIT_PERMISSIONS: tuple[tuple[str, str], ...] = (("audit:read", "查詢稽核紀錄"),)

AUDIT_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset({"audit:read"}),
    "admin": frozenset({"audit:read"}),
}

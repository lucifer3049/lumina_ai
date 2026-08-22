"""Platform context 的權限碼（04 §8.2，2A-3）。

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

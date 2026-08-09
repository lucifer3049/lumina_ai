"""測試用的系統資料重新種入（冪等）。

**為什麼需要它**：`transaction=True` 的測試在每條結束後會跑 Django 的 flush，
而 flush 是 ``TRUNCATE`` ——它會把 **migration 種進去的資料**（四個系統角色、
權限字典與它們的綁定）一起清掉。症狀很有代表性：同一個檔案裡第一條測試通過、
後面全部 `Role.DoesNotExist`，而單獨跑任何一條又都會過。

所以需要重新種入的測試要顯式呼叫本函式。**不做成 autouse**：那會讓「這份資料
從哪裡來」變得看不見，而且非 transactional 的測試根本不需要——它們看到的是
migration 的產物，那正是 `tests/integration/test_permission_seed.py` 要驗的東西
（若 autouse 把資料補回去，那條測試就再也驗不到 migration 有沒有寫對）。

內容從 `services/identity/permissions.py` 的常數展開，不另外抄一份：抄一份就是
第三個真相來源，而它與 migration 的差異不會有任何症狀。
"""

from __future__ import annotations

from django.db import connections

from services.identity.permissions import PERMISSION_CODES, SYSTEM_ROLE_PERMISSIONS


def ensure_identity_seed() -> None:
    """補回系統角色、權限字典與兩者的綁定。可重複呼叫。

    走 ``admin`` 連線（schema owner）：這些是跨租戶的系統資料，用應用角色寫入
    會受 RLS 的 ``WITH CHECK`` 限制——``tenant_id IS NULL`` 那一支雖然過得了，
    但語意上這是「平台資料」，用 owner 身分寫比較誠實。
    """
    roles = ", ".join(f"('{role}')" for role in SYSTEM_ROLE_PERMISSIONS)
    permissions = ", ".join(f"('{code}')" for code in sorted(PERMISSION_CODES))
    bindings = ", ".join(
        f"('{role}', '{code}')"
        for role, codes in SYSTEM_ROLE_PERMISSIONS.items()
        for code in sorted(codes)
    )

    with connections["admin"].cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO identity_role (id, tenant_id, name, is_system, created_at, updated_at)
            SELECT gen_random_uuid(), NULL, v.name, true, now(), now()
            FROM (VALUES {roles}) AS v(name)
            ON CONFLICT DO NOTHING;
            """  # noqa: S608 —— 值來自本專案常數，不含外部輸入
        )
        cursor.execute(
            f"""
            INSERT INTO identity_permission (id, code, description, created_at, updated_at)
            SELECT gen_random_uuid(), v.code, '', now(), now()
            FROM (VALUES {permissions}) AS v(code)
            ON CONFLICT (code) DO NOTHING;
            """  # noqa: S608
        )
        cursor.execute(
            f"""
            INSERT INTO identity_role_permission (role_id, permission_id, tenant_id, created_at)
            SELECT r.id, p.id, r.tenant_id, now()
            FROM (VALUES {bindings}) AS v(role_name, code)
            JOIN identity_role r ON r.name = v.role_name AND r.tenant_id IS NULL AND r.is_system
            JOIN identity_permission p ON p.code = v.code
            ON CONFLICT DO NOTHING;
            """  # noqa: S608
        )

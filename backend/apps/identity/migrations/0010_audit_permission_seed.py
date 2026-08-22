"""種入 audit 的權限碼並綁定系統角色（2A-4）。

形狀與 `0009_analytics_permission_seed.py` 完全相同，不重述理由（SQL 而非 ORM、新增
一支而不改寫前一支、字面值不 import 常數）。來源是
`services/platform/permissions.py`，對帳由 `tests/integration/test_permission_seed.py`。

**只綁 owner／admin**：界線同 analytics:read——admin 本來就在管使用者，
看「誰做了什麼」是同一份職務（該檔 docstring 的說明）。
"""

from __future__ import annotations

from django.db import migrations

# (code, 說明)
PERMISSIONS = (("audit:read", "查詢稽核紀錄"),)

# role name → codes
ROLE_PERMISSIONS = {
    "owner": ("audit:read",),
    "admin": ("audit:read",),
}

_CODES = tuple(code for code, _ in PERMISSIONS)


def _insert_permissions() -> str:
    values = ", ".join(f"('{code}', '{description}')" for code, description in PERMISSIONS)
    return f"""
        INSERT INTO identity_permission (id, code, description, created_at, updated_at)
        SELECT gen_random_uuid(), v.code, v.description, now(), now()
        FROM (VALUES {values}) AS v(code, description)
        ON CONFLICT (code) DO NOTHING;
    """


def _bind_roles() -> str:
    pairs = ", ".join(
        f"('{role}', '{code}')" for role, codes in ROLE_PERMISSIONS.items() for code in codes
    )
    return f"""
        INSERT INTO identity_role_permission (role_id, permission_id, tenant_id, created_at)
        SELECT r.id, p.id, r.tenant_id, now()
        FROM (VALUES {pairs}) AS v(role_name, code)
        JOIN identity_role r ON r.name = v.role_name AND r.tenant_id IS NULL AND r.is_system
        JOIN identity_permission p ON p.code = v.code
        ON CONFLICT DO NOTHING;
    """


def _unbind_roles() -> str:
    codes = ", ".join(f"'{code}'" for code in _CODES)
    return f"""
        DELETE FROM identity_role_permission rp
        USING identity_permission p
        WHERE rp.permission_id = p.id AND p.code IN ({codes});
    """


def _delete_permissions() -> str:
    codes = ", ".join(f"'{code}'" for code in _CODES)
    return f"DELETE FROM identity_permission WHERE code IN ({codes});"


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0009_analytics_permission_seed"),
    ]

    operations = [
        migrations.RunSQL(sql=_insert_permissions(), reverse_sql=_delete_permissions()),
        migrations.RunSQL(sql=_bind_roles(), reverse_sql=_unbind_roles()),
    ]

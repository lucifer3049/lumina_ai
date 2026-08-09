"""權限字典與系統角色的綁定（10 §3、1A-4）。

**只種現在真的有端點在用的碼**：種了卻沒有端點的權限碼無法驗證對錯（名字、
粒度、屬於哪個角色都只是猜），而且它會出現在權限管理介面上，讓人以為那個功能
已經存在。其餘的碼隨各自的工作包進來。

內容必須與 `services/identity/permissions.py` 的常數一致。兩份會漂，而漂掉時
完全沒有症狀——程式照跑，只是某個角色實際擁有的權限跟你以為的不一樣。對帳由
`tests/integration/test_permission_seed.py` 負責（它查 DB 實際的列再比對常數）。

用 SQL 而不是 ORM：data migration 走 ORM 會綁在「今天的 model 長相」上，
日後改欄位時這支舊 migration 會壞掉。
"""

from __future__ import annotations

from django.db import migrations

# (code, 說明)
PERMISSIONS = (
    ("user:read", "檢視租戶內的使用者"),
    ("user:write", "建立、更新、停用使用者"),
    ("tenant:read", "檢視本租戶資訊"),
    ("tenant:admin", "變更本租戶設定"),
)

# role name → codes
ROLE_PERMISSIONS = {
    "owner": ("user:read", "user:write", "tenant:read", "tenant:admin"),
    "admin": ("user:read", "user:write", "tenant:read"),
    "editor": ("tenant:read",),
    "viewer": ("tenant:read",),
}


def _insert_permissions() -> str:
    values = ", ".join(f"('{code}', '{description}')" for code, description in PERMISSIONS)
    # 值全部來自本檔上方的常數，沒有外部輸入（S608 由 pyproject 的 migrations
    # per-file-ignores 放行——migration 的 SQL 本來就是靜態拼出來的）。
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
    # tenant_id 跟著角色走：系統角色是 NULL（全租戶共用）。
    return f"""
        INSERT INTO identity_role_permission (role_id, permission_id, tenant_id, created_at)
        SELECT r.id, p.id, r.tenant_id, now()
        FROM (VALUES {pairs}) AS v(role_name, code)
        JOIN identity_role r ON r.name = v.role_name AND r.tenant_id IS NULL AND r.is_system
        JOIN identity_permission p ON p.code = v.code
        ON CONFLICT DO NOTHING;
    """


class Migration(migrations.Migration):
    dependencies = [("identity", "0004_auth_support")]

    operations = [
        migrations.RunSQL(
            sql=_insert_permissions(),
            reverse_sql="DELETE FROM identity_permission;",
        ),
        migrations.RunSQL(
            sql=_bind_roles(),
            reverse_sql=(
                "DELETE FROM identity_role_permission rp USING identity_role r "
                "WHERE rp.role_id = r.id AND r.tenant_id IS NULL AND r.is_system;"
            ),
        ),
    ]

"""種入 knowledge 的權限碼並綁定系統角色（1B-2）。

形狀與 `0005_permission_seed.py` 完全相同（SQL 而非 ORM，理由見該檔）。分成兩支
migration 而不是改寫 0005：既有環境已經套用過 0005，改寫它不會重跑，而「本機重建
資料卷時是對的、既有環境是舊的」是最難察覺的一種漂移。

碼與角色綁定的**來源仍是 `services/knowledge/permissions.py`**，這裡是它在 DB 的
複本。兩份會漂，對帳由 `tests/integration/test_permission_seed.py` 負責（它查 DB 實際
的列再比對匯總後的常數）。**這裡刻意寫死字面值而不 import 常數**：data migration
一旦 import 應用程式碼，就綁在「今天的常數長相」上——日後常數改名或搬家，這支已經
跑過的 migration 會在全新環境重播時失敗。
"""

from __future__ import annotations

from django.db import migrations

# (code, 說明)
PERMISSIONS = (
    ("knowledge:read", "檢視知識庫與其中的文件"),
    ("knowledge:write", "上傳與刪除文件"),
    ("knowledge:admin", "建立與刪除知識庫、變更切塊與檢索設定"),
)

# role name → codes（2026-08-09 產品決策，見 services/knowledge/permissions.py）
ROLE_PERMISSIONS = {
    "owner": ("knowledge:read", "knowledge:write", "knowledge:admin"),
    "admin": ("knowledge:read", "knowledge:write", "knowledge:admin"),
    "editor": ("knowledge:read", "knowledge:write"),
    "viewer": ("knowledge:read",),
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
    # tenant_id 跟著角色走：系統角色是 NULL（全租戶共用）。
    return f"""
        INSERT INTO identity_role_permission (role_id, permission_id, tenant_id, created_at)
        SELECT r.id, p.id, r.tenant_id, now()
        FROM (VALUES {pairs}) AS v(role_name, code)
        JOIN identity_role r ON r.name = v.role_name AND r.tenant_id IS NULL AND r.is_system
        JOIN identity_permission p ON p.code = v.code
        ON CONFLICT DO NOTHING;
    """


def _unbind_roles() -> str:
    """反向只刪 **本次種進去的碼**的綁定。

    0005 的反向是無條件 `DELETE FROM identity_permission`——那在只有一支 seed 時等價，
    現在會把 identity 自己的碼一起刪掉。回滾一支 migration 不該影響另一支的資料。
    """
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
    dependencies = [("identity", "0005_permission_seed")]

    operations = [
        migrations.RunSQL(sql=_insert_permissions(), reverse_sql=_delete_permissions()),
        migrations.RunSQL(sql=_bind_roles(), reverse_sql=_unbind_roles()),
    ]

"""驗收：資料庫裡的權限資料與程式裡的對應表一致（10 §3）。

權限的定義必然存在兩份——`services/identity/permissions.py` 的常數（程式判定用）
與 migration 種進 DB 的列（角色指派與管理介面用）。兩份會漂，而漂掉時**沒有任何
症狀**：程式照跑，只是某個角色實際擁有的權限跟你以為的不一樣。

這裡不比對「兩段程式碼看起來一樣」（那只證明抄對了），而是查 **DB 上實際的列**
再跟常數比。忘了跑 migration、migration 寫錯、或有人手動改過資料，都會在這裡紅燈。

同樣的形狀已經用在 `test_db_timeouts.py`（Makefile 的值 vs DB 生效值）。
"""

from __future__ import annotations

import pytest
from django.db import connection

from services.identity.permissions import PERMISSION_CODES, SYSTEM_ROLE_PERMISSIONS

pytestmark = pytest.mark.django_db(databases=["default", "admin"])


def _fetch(sql: str) -> list[tuple[object, ...]]:
    """走 admin 連線：`identity_permission` 沒有 RLS，但 `identity_role` 有，
    而這裡要看的是**全部**系統角色，不是某個租戶視角下的子集。
    """
    from django.db import connections

    with connections["admin"].cursor() as cursor:
        cursor.execute(sql)
        return list(cursor.fetchall())


def test_permission_dictionary_rows_match_the_constant() -> None:
    rows = {str(code) for (code,) in _fetch("SELECT code FROM identity_permission")}

    assert rows == PERMISSION_CODES, (
        f"DB 的權限字典與常數不一致：多了 {sorted(rows - PERMISSION_CODES)}、"
        f"少了 {sorted(PERMISSION_CODES - rows)}"
    )


def test_system_role_bindings_match_the_matrix() -> None:
    """每個系統角色在 DB 裡綁到的權限碼，必須逐一等於對應表。

    系統角色的 ``tenant_id`` 是 NULL（全租戶共用），所以這裡不帶租戶條件——
    查到的就是所有租戶會看到的那一份。
    """
    rows = _fetch(
        """
        SELECT r.name, p.code
        FROM identity_role r
        JOIN identity_role_permission rp ON rp.role_id = r.id
        JOIN identity_permission p ON p.id = rp.permission_id
        WHERE r.tenant_id IS NULL AND r.is_system
        """
    )

    actual: dict[str, set[str]] = {}
    for role_name, code in rows:
        actual.setdefault(str(role_name), set()).add(str(code))

    expected = {str(role): set(codes) for role, codes in SYSTEM_ROLE_PERMISSIONS.items()}
    # 沒有任何權限的角色在 join 之後不會出現，補成空集合才比得起來。
    for role in expected:
        actual.setdefault(role, set())

    assert actual == expected


def test_system_roles_have_no_tenant_specific_duplicates() -> None:
    """不得出現「某個租戶自己的 owner 角色」。

    系統角色的保證是「所有租戶共用同一份、不可修改」。若哪天有程式在某個租戶下
    建了同名角色，權限判定會依查詢順序時而拿到這份、時而拿到那份——症狀是
    「同一個人有時有權限、有時沒有」，極難重現。
    """
    duplicates = _fetch(
        """
        SELECT name, count(*) FROM identity_role
        WHERE name IN ('owner', 'admin', 'editor', 'viewer')
        GROUP BY name HAVING count(*) > 1
        """
    )

    assert not duplicates, f"系統角色名稱出現租戶級複本：{duplicates}"


def test_permission_table_is_still_global() -> None:
    """字典表不得長出 tenant_id（1A-2 的決定在這裡再確認一次）。

    這條與 `test_rls_identity.py` 的那條重疊是刻意的：那邊從 RLS 的角度看，
    這邊從權限資料的角度看。加了 tenant_id 之後每個租戶要一份複本，而
    `SYSTEM_ROLE_PERMISSIONS` 這個全域常數就再也對不起來了。
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'identity_permission' AND column_name = 'tenant_id'"
        )
        assert cursor.fetchone() is None

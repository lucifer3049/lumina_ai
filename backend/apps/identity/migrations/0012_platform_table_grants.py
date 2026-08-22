"""兩張平台級表對應用角色收回寫入權（2026-08-22 審查缺口）。

`identity_permission`（全域權限字典）與 `identity_tenant_directory`（登入路由表）都
**沒有也不該有 RLS**——前者沒有 tenant_id，開了會擋掉所有權限查詢；後者要在「還不知道
是哪個租戶」的時候被查（10 §2.1 的 slug → tenant_id）。理由見
`tests/integration/test_rls_identity.py::test_permissions_is_intentionally_global`。

於是它們少了第二道防線，而 `docker/postgres/initdb.d/10-roles.sh` 的 default privileges
給了應用角色全套 ``INSERT/UPDATE/DELETE``（那是給業務表用的一次性設定，不分表）。兩張表
的正當寫入者其實只有兩個，都不是應用連線：

* migration（owner）——種權限碼、建目錄表的既有列。
* `identity_sync_tenant_directory()` 這個 ``SECURITY DEFINER`` trigger（`0004_auth_support`）
  ——它以函式擁有者（owner）的身分寫入，與呼叫端的角色無關，因此不受本檔影響。

所以把寫入權從應用角色收回，讓「應用連線寫壞全域權限字典或登入路由」在 DB 這一層就不
可能。``SELECT`` 保留：兩張表都在登入與權限判定的熱路徑上。

**為什麼不寫在 10-roles.sh**：那支腳本只在資料卷首次初始化時跑，那時這兩張表還不存在
（表由 migration 建立），REVOKE 會直接報「關聯不存在」。default privileges 也幫不上忙
——它套用在 CREATE TABLE 的當下，無法針對個別表例外。

**為什麼包在 `pg_roles` 的存在檢查裡**：角色名來自環境變數（`DB_USER`），而 CI 之外還有
「直接對既有資料庫跑 migration」的情境；角色不存在時應該跳過而不是讓整包 migration 停在
一半。
"""

from __future__ import annotations

import re
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import migrations

# 應用角色寫入權要收回的表。
TABLES = ("identity_permission", "identity_tenant_directory")

WRITE_PRIVILEGES = "INSERT, UPDATE, DELETE"

# 角色名會被拼進 SQL（識別字無法參數化），所以先把它限制在安全字元集內。這不是理論上的
# 潔癖：`DB_USER` 是環境變數，部署時打錯或帶入引號的話，錯誤訊息會是難懂的語法錯誤。
_SAFE_ROLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _app_role() -> str:
    role = str(settings.DATABASES["default"]["USER"])
    if not _SAFE_ROLE.match(role):
        raise ImproperlyConfigured(f"DB_USER 只允許英數與底線（實際值：{role!r}）")
    return role


def _guarded(statement: str) -> str:
    """把一句 GRANT / REVOKE 包進「角色存在才做」的檢查裡。"""
    return f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_app_role()}') THEN
                {statement};
            END IF;
        END $$;
    """


def _revoke_writes(apps: Any, schema_editor: Any) -> None:
    tables = ", ".join(TABLES)
    schema_editor.execute(_guarded(f'REVOKE {WRITE_PRIVILEGES} ON {tables} FROM "{_app_role()}"'))


def _restore_writes(apps: Any, schema_editor: Any) -> None:
    tables = ", ".join(TABLES)
    schema_editor.execute(_guarded(f'GRANT {WRITE_PRIVILEGES} ON {tables} TO "{_app_role()}"'))


class Migration(migrations.Migration):
    dependencies = [("identity", "0011_rls_write_scope")]

    operations = [migrations.RunPython(_revoke_writes, _restore_writes)]

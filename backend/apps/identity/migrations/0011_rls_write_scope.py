"""全租戶共用的列只讀不寫（2026-08-22 審查缺口；補 0002_rls）。

`0002_rls` 的 policy 是 ``FOR ALL``，而 ``USING`` 為了讓系統內建角色對所有租戶可見
放行了 ``tenant_id IS NULL``。**那個放行同時放行了寫的方向**——PostgreSQL 的
``FOR ALL`` policy 裡，DELETE 只看 ``USING``，UPDATE 看 ``USING``（舊列）加
``WITH CHECK``（新列）：

    DELETE FROM identity_role_permission WHERE tenant_id IS NULL;

一條就把全平台所有租戶的權限判定同時清空（授權列沒有任何 FK 保護），而執行它只需要
應用角色的連線加任一租戶 context——也就是任何一個未來的程式 bug 或 SQL injection 的
落點。UPDATE 更安靜：把系統角色的 ``tenant_id`` 改成自己，舊列過 ``USING``（NULL 放
行）、新列過 ``WITH CHECK``（是自己的租戶），於是那個角色從所有其他租戶手上消失。

處置是把 ``FOR ALL`` 拆成 per-command，讀寬寫窄：

* ``FOR SELECT``——``tenant_id IS NULL OR tenant_id = 當前租戶``（維持 0002 的可見範圍，
  系統角色仍對所有租戶可見）。
* ``FOR UPDATE`` / ``FOR DELETE``——``USING`` 只放行自己的租戶。共用的列於是在改與刪
  的視角下**不存在**，影響 0 列（RLS 的擋法是讓列消失，不是報錯）。
* ``FOR INSERT``——**維持 0002 的條件**（NULL 也放行）。收窄它是另一件事：identity 至今
  沒有任何寫入端點，而 `tests/factories/identity.py` 的 `SystemRoleFactory` 正是以應用
  角色建立 ``tenant_id IS NULL`` 的角色。等 `/roles` 寫入端點進來時（那時才有真的呼叫
  端要擋）再一併收，連同 ai_prompt 已經在用的不對稱形狀。

**owner 的 `system_roles` policy**（形狀與理由同 `apps/ai/migrations/0002_rls.py` 的
`system_templates`）：``FORCE ROW LEVEL SECURITY`` 讓 migration 自己也受管，而 UPDATE /
DELETE 一旦收窄，`0004_auth_support` 與 `0005_permission_seed` 的 ``reverse_sql``
（刪掉四個系統角色與它們的授權列）就會**安靜地刪 0 列**——rollback 看起來成功，資料
還在。日後補綁新的權限碼、改系統角色名稱也是同一條路。所以共用列的寫入權從「任何租
戶」改為「只有部署」，而不是「沒有人」。

沒有對應 command 的 policy 即拒絕，所以四條缺一不可——漏掉 ``FOR INSERT`` 的症狀是
建立租戶自訂角色時 ``new row violates row-level security policy``。
"""

from __future__ import annotations

from django.db import migrations

# 與 0002_rls 同一份寫法，不要漂掉。
CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"

SHARED_OR_MINE = f"tenant_id IS NULL OR tenant_id = {CURRENT_TENANT}"
MINE_ONLY = f"tenant_id = {CURRENT_TENANT}"

# 只有這兩張表的 policy 放行 NULL（0002_rls 的例外）；其餘 identity 表不受本檔影響。
TABLES = ("identity_role", "identity_role_permission")


def _split(table: str) -> str:
    return f"""
        DROP POLICY IF EXISTS tenant_isolation ON {table};

        CREATE POLICY tenant_read ON {table}
            FOR SELECT USING ({SHARED_OR_MINE});
        CREATE POLICY tenant_insert ON {table}
            FOR INSERT WITH CHECK ({SHARED_OR_MINE});
        CREATE POLICY tenant_update ON {table}
            FOR UPDATE USING ({MINE_ONLY}) WITH CHECK ({MINE_ONLY});
        CREATE POLICY tenant_delete ON {table}
            FOR DELETE USING ({MINE_ONLY});

        DROP POLICY IF EXISTS system_roles ON {table};
        CREATE POLICY system_roles ON {table} TO CURRENT_USER
            USING (true)
            WITH CHECK (true);
    """


def _restore(table: str) -> str:
    """回到 0002_rls 的單一 `FOR ALL` policy（含它的漏洞——rollback 要回得去原狀）。"""
    return f"""
        DROP POLICY IF EXISTS system_roles ON {table};
        DROP POLICY IF EXISTS tenant_delete ON {table};
        DROP POLICY IF EXISTS tenant_update ON {table};
        DROP POLICY IF EXISTS tenant_insert ON {table};
        DROP POLICY IF EXISTS tenant_read ON {table};

        CREATE POLICY tenant_isolation ON {table}
            USING ({SHARED_OR_MINE})
            WITH CHECK ({SHARED_OR_MINE});
    """


class Migration(migrations.Migration):
    dependencies = [("identity", "0010_audit_permission_seed")]

    operations = [
        migrations.RunSQL(sql=_split(table), reverse_sql=_restore(table)) for table in TABLES
    ]

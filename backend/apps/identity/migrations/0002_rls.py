"""RLS policy —— 租戶隔離的第二道防線（05 §5.1、13 §3 工作包 1A）。

第一道是 Repository 的 tenant filter，它只保證「今天的程式寫對了」；這裡保證
「明天有人漏寫 filter，資料仍然不會外洩」。

**四個必須同時成立的條件**，缺任何一個 policy 都等於沒開，而且全部無症狀：

1. ``ENABLE ROW LEVEL SECURITY``——開啟。
2. ``FORCE ROW LEVEL SECURITY``——連表的 owner 也受管。少了它，跑 migration 與
   維運腳本的 `lumina_owner` 讀寫時完全不受 policy 約束。
3. ``USING``——決定看得到哪些列（SELECT / UPDATE / DELETE 的可見範圍）。
4. ``WITH CHECK``——決定寫進去的列長什麼樣。只寫 ``USING`` 的 policy 擋得住讀、
   擋不住把資料寫進別的租戶名下，而讀取測試永遠看不到這個漏洞。

再加上連線角色必須非 superuser、非 BYPASSRLS、非表 owner——那是 1A-1 做的事
（docker/postgres/initdb.d/10-roles.sh）。

**租戶值的取法**：``nullif(current_setting('app.tenant_id', true), '')::uuid``。
``true`` 是 missing_ok——參數沒設過時回 NULL 而不是報錯；``nullif`` 處理另一種
「設過但已隨交易結束還原」的情況（那時是空字串）。兩者都會讓整個條件變成 NULL，
於是一列都不符合，也就是 fail closed。

為什麼不讓它報錯（1A-2 決定，選項 A）：真正該擋下「忘了設租戶」的是
`core/uow.py`，它已經直接 raise。DB 這層是最後一張網，網子的正確行為是「什麼都
不給看」；報錯的話 ``make psql-app`` 這種手動連線一進去就爆，而錯誤訊息
（``invalid input syntax for type uuid``）跟租戶毫無關係。

**`identity_role` 與 `identity_role_permission` 的例外**：``tenant_id IS NULL``
代表全租戶共用的系統內建角色，必須對所有租戶可見。照一般形狀寫的話 NULL 與任何
值比較都是 NULL 而非 true，四個系統角色會對所有人隱形——症狀是使用者明明有角色
卻查不到，權限判定安靜地退化成「什麼都不能做」。

`identity_permission` 不在本檔：它是全域字典表，沒有 tenant_id，不需要也不能開
RLS（開了會擋掉所有查詢）。
"""

from __future__ import annotations

from django.db import migrations

# 交易區域參數的讀法，五張表共用一份，避免其中一張寫法漂掉。
CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"

# (表名, policy 條件)。`identity_tenant` 比對 id——它自己就是租戶。
TABLE_PREDICATES = (
    ("identity_tenant", f"id = {CURRENT_TENANT}"),
    ("identity_user", f"tenant_id = {CURRENT_TENANT}"),
    # 系統內建角色（tenant_id IS NULL）全租戶共用，見本檔 docstring。
    ("identity_role", f"tenant_id IS NULL OR tenant_id = {CURRENT_TENANT}"),
    ("identity_user_role", f"tenant_id = {CURRENT_TENANT}"),
    ("identity_role_permission", f"tenant_id IS NULL OR tenant_id = {CURRENT_TENANT}"),
)


def _enable(table: str, predicate: str) -> str:
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
            USING ({predicate})
            WITH CHECK ({predicate});
    """


def _disable(table: str) -> str:
    return f"""
        DROP POLICY IF EXISTS tenant_isolation ON {table};
        ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
    """


class Migration(migrations.Migration):
    dependencies = [("identity", "0001_initial")]

    operations = [
        migrations.RunSQL(sql=_enable(table, predicate), reverse_sql=_disable(table))
        for table, predicate in TABLE_PREDICATES
    ]

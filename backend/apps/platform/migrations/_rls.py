"""RLS 的 SQL 產生器 —— 本 app 的 migration 與分區維護（repositories/partitions.py）共用。

內容與 `apps/conversation/migrations/_rls.py` 完全相同（每個 app 一份是既有慣例，
identity／knowledge／conversation 皆如此）。**共用而不是散寫**的理由在分區表上最尖銳：
分區是會一直新增的東西（Beat 每月建一批），每一個新分區都要套上與父表完全相同的
規則——兩份 SQL 會漂，而漂掉的症狀是「某幾個月份的帳沒有隔離」，沒有錯誤訊息。

底線：`true` 是 missing_ok（沒設過時回 NULL 而非報錯）；`nullif` 處理「設過但已隨
交易結束還原」的空字串。兩者都讓條件變成 NULL，於是一列都不符合——fail closed。
"""

from __future__ import annotations

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"


def enable(table: str) -> str:
    """啟用 + FORCE + 建 policy。

    `ENABLE` 之外還要 `FORCE`：owner 建的表對 owner 預設豁免 policy（13 §3.1），
    而 migration 正是以 owner 角色執行的。
    """
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_isolation ON {table};
        CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = {CURRENT_TENANT})
            WITH CHECK (tenant_id = {CURRENT_TENANT});
    """


def disable(table: str) -> str:
    return f"""
        DROP POLICY IF EXISTS tenant_isolation ON {table};
        ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
    """

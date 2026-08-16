"""RLS 的 SQL 產生器 —— 由 `0001_initial`（分區）與 `0002_rls`（三張表）共用。

**共用而不是各寫一份**：分區是會一直新增的東西（2A 的 Beat 每月建一批），而每一個
新分區都要套上與父表**完全相同**的規則。兩份會漂，而漂掉的症狀是「某幾個月份的訊息
沒有隔離」——沒有錯誤訊息，且只有直接查那個子分區時才看得到。

底線與 identity/knowledge 的 `0002_rls.py` 一致：`true` 是 missing_ok（沒設過時回 NULL
而非報錯）；`nullif` 處理「設過但已隨交易結束還原」的空字串。兩者都讓條件變成 NULL，
於是一列都不符合——fail closed。
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

"""RLS policy —— AI context 兩張表（05 §5.1、13 §3 工作包 1D-3b）。

四個必須同時成立的條件、租戶值的取法、為什麼 fail closed 而不是報錯——理由見
`apps/identity/migrations/0002_rls.py`，不重述。

**這一組比前面幾個 context 多兩件事，兩件都是為了系統模板（`tenant_id IS NULL`）。**

### 一、讀寫條件不對稱

系統模板要讓**所有**租戶讀得到，所以 `USING` 必須放行 NULL。但若 `WITH CHECK` 照抄
同一個條件，租戶就**寫得出** `tenant_id IS NULL` 的列——那不是資料外洩，是跨租戶的
指令注入：

    A 建立一份 tenant_id = NULL 的模板
      → B 問問題時解析到它
      → B 的 LLM 照著 A 寫的指令回答 B 的問題

而 B 完全看不出來。`identity_role` 那組讀寫用同一個條件（那裡沒有寫入端點，所以不
成問題），這裡不能照抄——09 §2.5 的 `/prompts` 在 Phase 5 就是寫入端點。

所以：**讀得到共用的，只寫得進自己的。**

### 二、owner 專用的第二條 policy

不對稱之後，種子 migration 反而寫不進去：它寫的正是一列 `tenant_id IS NULL`，而
migration 跑在 owner 身上、`FORCE ROW LEVEL SECURITY` 讓 owner 也要守 policy
（13 §3.1 刻意如此，否則 owner 是後門）。

處置是給 owner 一條自己的 policy。**PostgreSQL 的多條 policy 是 OR 起來的**，而
policy 可以限定角色，因此租戶那條維持窄的，owner 那條放行——結果是「誰能建立系統
模板」的答案變成**只有一次部署**，而那正是 06 §3.5 要的（模板變更走 review）。

`TO CURRENT_USER` 而不是寫死角色名：角色名來自環境變數（`DB_ADMIN_USER`，見
docker/postgres/initdb.d/10-roles.sh），寫死的話換個環境就建不起來。CREATE POLICY 在
**建立當下**把 CURRENT_USER 解析成實際的角色，而 migration 一定是 owner 在跑。

### 三、`ai_promptversion` 沒有 tenant_id

它經 `prompt_id` 隸屬於 Prompt，因此 policy 是 `EXISTS (...)` 形式。**模板本體在這張
表**——prompts 擋住了而 versions 沒擋，等於門鎖了窗開著，而洩漏的是內容最敏感的一半
（prompts 只有 key 與標題）。

子查詢本身同樣受 `ai_prompt` 的 policy 約束，所以這裡不必重複寫租戶條件；寫入端
（`WITH CHECK`）刻意要求 `p.tenant_id = 當前租戶`：加版本給一份系統模板，與建立系統
模板是同一件事。
"""

from __future__ import annotations

from django.db import migrations

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"

# (表名, USING 條件, WITH CHECK 條件)。不對稱是本檔的重點，見 docstring。
TABLE_POLICIES = (
    (
        "ai_prompt",
        f"tenant_id IS NULL OR tenant_id = {CURRENT_TENANT}",
        f"tenant_id = {CURRENT_TENANT}",
    ),
    (
        "ai_promptversion",
        "EXISTS (SELECT 1 FROM ai_prompt p WHERE p.id = prompt_id)",
        f"EXISTS (SELECT 1 FROM ai_prompt p WHERE p.id = prompt_id"
        f" AND p.tenant_id = {CURRENT_TENANT})",
    ),
)


def _enable(table: str, using: str, check: str) -> str:
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_isolation ON {table};
        CREATE POLICY tenant_isolation ON {table}
            USING ({using})
            WITH CHECK ({check});
        DROP POLICY IF EXISTS system_templates ON {table};
        CREATE POLICY system_templates ON {table} TO CURRENT_USER
            USING (true)
            WITH CHECK (true);
    """


def _disable(table: str) -> str:
    return f"""
        DROP POLICY IF EXISTS system_templates ON {table};
        DROP POLICY IF EXISTS tenant_isolation ON {table};
        ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
    """


class Migration(migrations.Migration):
    dependencies = [("ai", "0001_initial")]

    operations = [
        migrations.RunSQL(sql=_enable(table, using, check), reverse_sql=_disable(table))
        for table, using, check in TABLE_POLICIES
    ]

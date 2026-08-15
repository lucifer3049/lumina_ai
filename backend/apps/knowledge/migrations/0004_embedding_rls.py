"""RLS policy —— `knowledge_embedding`（05 §5.1、13 §3 工作包 1C-2）。

形狀與 `0002_rls.py` 相同，理由不重述。**新表一律要補這一步**：`0002_rls` 只涵蓋它
當時存在的四張表，而漏掉的表不會有任何症狀——查詢照常回傳，只是回傳的範圍變成整個
資料庫。守門在 `tests/integration/test_infra_postgres.py`（有表啟用 RLS 就斷言連線
角色非 superuser 且已 FORCE）與 `test_embeddings.py::test_rls_is_enabled_and_forced`。

embeddings 的洩漏後果與 chunks 相同：別家的內容被當成本租戶的答案來源餵進 LLM。
差別只在它是向量——而向量正是檢索**實際比對**的東西，chunk 的文字只是事後拿來顯示的。
"""

from __future__ import annotations

from django.db import migrations

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"

TABLE = "knowledge_embedding"

ENABLE = f"""
    ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON {TABLE}
        USING (tenant_id = {CURRENT_TENANT})
        WITH CHECK (tenant_id = {CURRENT_TENANT});
"""

DISABLE = f"""
    DROP POLICY IF EXISTS tenant_isolation ON {TABLE};
    ALTER TABLE {TABLE} NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY;
"""


class Migration(migrations.Migration):
    dependencies = [("knowledge", "0003_embedding")]

    operations = [migrations.RunSQL(sql=ENABLE, reverse_sql=DISABLE)]

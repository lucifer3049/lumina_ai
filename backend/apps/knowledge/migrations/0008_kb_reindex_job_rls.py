"""`knowledge_kbreindexjob` 的 RLS policy（05 §5.1、工作包 2B-6）。

形狀與 `0002_rls.py` / `0004_embedding_rls.py` 逐字相同（條件的取法與 fail closed
的理由見 0002，不重述）。**分成獨立一支 migration** 而不是塞進 0007：0007 是
`makemigrations` 產生的，下一次自動產生時把手寫的 RunSQL 蓋掉不會有任何提示。

這張表沒有文件內容，但**漏開 RLS 一樣是資料外洩**：`target_model` 洩漏的是別家用
哪個 embedding 模型，而 job 的存在本身洩漏「那個租戶有哪些 KB、什麼時候重建過」。
更實際的是寫入端——少了 policy，一個漏帶 tenant filter 的 UPDATE 會把別家進行中的
job 標成 completed，於是那個 KB 會在向量還沒算完時被切換過去。
"""

from __future__ import annotations

from django.db import migrations

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"

TABLE = "knowledge_kbreindexjob"

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
    dependencies = [("knowledge", "0007_kb_reindex_job")]

    operations = [migrations.RunSQL(sql=ENABLE, reverse_sql=DISABLE)]

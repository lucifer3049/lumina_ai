"""RLS policy —— Knowledge 四張表（05 §5.1、13 §3 工作包 1B）。

形狀與 `apps/identity/migrations/0002_rls.py` 完全相同，理由不重述（見該檔：四個
必須同時成立的條件、租戶值的取法、為什麼 fail closed 而不是報錯）。

**這裡的表比 identity 那組更值得盯**：`knowledge_chunk` 是 RAG 檢索實際讀的表。
identity 洩漏是「看到別家的使用者清單」；chunk 洩漏是「別家的文件內容被當成本租戶
的答案來源餵進 LLM，再連同引用一起回給使用者」——同時是資料外洩與答案汙染，而回應
看起來完全正常。

四張表都沒有 identity `Role` 那種 ``tenant_id IS NULL`` 的例外：knowledge 沒有
「全租戶共用」的資料。
"""

from __future__ import annotations

from django.db import migrations

# 交易區域參數的讀法，四張表共用一份，避免其中一張寫法漂掉。
# `true` 是 missing_ok（沒設過時回 NULL 而非報錯）；`nullif` 處理「設過但已隨交易
# 結束還原」的空字串。兩者都讓條件變成 NULL，於是一列都不符合——fail closed。
CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"

TABLES = (
    "knowledge_knowledgebase",
    "knowledge_document",
    "knowledge_chunk",
    "knowledge_etljob",
)


def _enable(table: str) -> str:
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = {CURRENT_TENANT})
            WITH CHECK (tenant_id = {CURRENT_TENANT});
    """


def _disable(table: str) -> str:
    return f"""
        DROP POLICY IF EXISTS tenant_isolation ON {table};
        ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
    """


class Migration(migrations.Migration):
    dependencies = [("knowledge", "0001_initial")]

    operations = [
        migrations.RunSQL(sql=_enable(table), reverse_sql=_disable(table)) for table in TABLES
    ]

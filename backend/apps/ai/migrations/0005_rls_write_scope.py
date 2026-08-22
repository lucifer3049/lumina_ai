"""系統模板只讀不寫（2026-08-22 審查缺口；補 0002_rls）。

`0002_rls` 的 docstring 說「誰能建立系統模板的答案是只有一次部署」——**建立**確實擋住
了（``WITH CHECK`` 比 ``USING`` 窄），但那個 policy 是 ``FOR ALL``，而 ``FOR ALL`` 裡：

* DELETE 只檢查 ``USING``——而 ``USING`` 為了讓所有租戶讀得到系統模板放行了
  ``tenant_id IS NULL``。於是任一租戶的連線 ``DELETE FROM ai_prompt WHERE tenant_id IS
  NULL`` 就刪掉了全平台的系統模板，且 `PromptVersion.prompt` 是 ``on_delete=CASCADE``
  ——FK 級聯**不受 RLS 約束**，模板內容一起消失。症狀是所有租戶的問答同時找不到模板。
* UPDATE 檢查 ``USING``（舊列）+ ``WITH CHECK``（新列），而
  ``UPDATE ai_prompt SET tenant_id = '<我>' WHERE tenant_id IS NULL`` 兩邊都過：舊列因
  NULL 放行，新列因為是自己的租戶而合法。系統模板被劫持成某租戶私有，其他租戶瞬間
  找不到模板——而資料庫裡沒有任何一列被刪。

所以保證只兌現了三分之一。處置是把 ``FOR ALL`` 拆成 per-command，讀寬寫窄：

    SELECT  → 系統模板 + 自己的（維持 0002 的可見範圍）
    INSERT  → 只有自己的（維持 0002 的 `WITH CHECK`）
    UPDATE  → `USING` 與 `WITH CHECK` 都只有自己的
    DELETE  → `USING` 只有自己的

`ai_promptversion` 一起拆，理由同 0002：**模板本體在那張表**。它的 UPDATE 其實原本就
擋住了（``WITH CHECK`` 要求父列屬於自己），但 DELETE 只看 ``USING``——那是門鎖了、窗開
著的另一半，而且刪掉的正是內容。

owner 的 `system_templates` policy 不動（多條 policy 是 OR 起來的）：種模板、改
``active_version_id`` 仍然只有部署做得到，`tests/seed.py` 的補種也走那條。
"""

from __future__ import annotations

from django.db import migrations

# 與 0002_rls 同一份寫法，不要漂掉。
CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"

PROMPT_SHARED_OR_MINE = f"tenant_id IS NULL OR tenant_id = {CURRENT_TENANT}"
PROMPT_MINE_ONLY = f"tenant_id = {CURRENT_TENANT}"

# 版本表沒有 tenant_id，隔離經 prompt_id 走父列（0002 §三）。子查詢本身受 ai_prompt 的
# policy 約束，所以讀的那一條不必重複寫租戶條件；寫的三條刻意要求父列屬於自己。
VERSION_VISIBLE = "EXISTS (SELECT 1 FROM ai_prompt p WHERE p.id = prompt_id)"
VERSION_MINE_ONLY = (
    f"EXISTS (SELECT 1 FROM ai_prompt p WHERE p.id = prompt_id AND p.tenant_id = {CURRENT_TENANT})"
)

# (表名, 讀條件, 寫條件)
TABLE_POLICIES = (
    ("ai_prompt", PROMPT_SHARED_OR_MINE, PROMPT_MINE_ONLY),
    ("ai_promptversion", VERSION_VISIBLE, VERSION_MINE_ONLY),
)


def _split(table: str, read: str, write: str) -> str:
    return f"""
        DROP POLICY IF EXISTS tenant_isolation ON {table};

        CREATE POLICY tenant_read ON {table}
            FOR SELECT USING ({read});
        CREATE POLICY tenant_insert ON {table}
            FOR INSERT WITH CHECK ({write});
        CREATE POLICY tenant_update ON {table}
            FOR UPDATE USING ({write}) WITH CHECK ({write});
        CREATE POLICY tenant_delete ON {table}
            FOR DELETE USING ({write});
    """


def _restore(table: str, read: str, write: str) -> str:
    """回到 0002_rls 的單一 `FOR ALL` policy（含它的漏洞——rollback 要回得去原狀）。"""
    return f"""
        DROP POLICY IF EXISTS tenant_delete ON {table};
        DROP POLICY IF EXISTS tenant_update ON {table};
        DROP POLICY IF EXISTS tenant_insert ON {table};
        DROP POLICY IF EXISTS tenant_read ON {table};

        CREATE POLICY tenant_isolation ON {table}
            USING ({read})
            WITH CHECK ({write});
    """


class Migration(migrations.Migration):
    dependencies = [("ai", "0004_seed_rag_prompt")]

    operations = [
        migrations.RunSQL(sql=_split(table, read, write), reverse_sql=_restore(table, read, write))
        for table, read, write in TABLE_POLICIES
    ]

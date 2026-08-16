"""Conversation 三張表的 RLS（05 §5.1）。

形狀與 identity／knowledge 的 `0002_rls.py` 相同，只有兩點差異值得記：

1. **SQL 由 `_rls.py` 共用**（分區也要用同一份），而不是像前兩個 app 那樣就地寫。
2. **`conversation_message` 是分區表**：這裡建的 policy 保護「經由父表」的存取，也就是
   Django 走的路徑；每個**子分區**另外在 `0001_initial` 建立時就套上同一份規則。
   兩處都要，理由見 `_rls.py` 的 docstring。

拆成獨立 migration 而不是併進 0001：與前兩個 app 一致，讓「表的形狀」與「隔離規則」
分開審——後者是安全性變更，值得單獨看。
"""

from __future__ import annotations

from django.db import migrations

from . import _rls

TABLES = (
    "conversation_conversation",
    "conversation_message",
    "conversation_memorysnapshot",
)


class Migration(migrations.Migration):
    dependencies = [("conversation", "0001_initial")]

    operations = [
        migrations.RunSQL(sql=_rls.enable(table), reverse_sql=_rls.disable(table))
        for table in TABLES
    ]

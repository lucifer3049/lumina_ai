"""`platform_credential` 與 `platform_tenantdatakey` 的 RLS（05 §5.1，2C-2）。

形狀與本 app 既有的 RLS migration 相同，共用 `_rls.py` 的產生器（每個 app 一份是既有
慣例）。**分成獨立一支**而不是塞進 0007：那一支是 `makemigrations` 產生的，下一次
自動產生時把手寫的 RunSQL 蓋掉不會有任何提示。

**這兩張表是全 repo 最不能漏的兩張**：漏了 RLS，一次寫錯 tenant 條件的查詢就能撈到
別的租戶的加密憑證與 DEK。密文本身還有一層保護（DEK 不同），但 DEK 那張表如果一起
漏，兩層就同時沒了——而查詢照樣回 200。
"""

from __future__ import annotations

from django.db import migrations

from apps.platform.migrations import _rls

TABLES = ("platform_credential", "platform_tenantdatakey")


class Migration(migrations.Migration):
    dependencies = [("platform", "0007_credentials")]

    operations = [
        migrations.RunSQL(sql=_rls.enable(table), reverse_sql=_rls.disable(table))
        for table in TABLES
    ]

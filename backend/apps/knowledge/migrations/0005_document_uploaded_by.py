# documents.uploaded_by（2A-5）：通知要知道寄給誰。
#
# **三步走的第一步**（CLAUDE.md 鐵則 7）：可為 NULL 的新欄位，既有列不動、
# 不需要回填也不加約束。舊文件因此永遠是 NULL，而通知對那些文件退回寄給
# 租戶的 owner/admin——「沒有人收到」比「收件人不精確」糟得多。
# 表不大（每租戶的文件數量級是千），AddField 的短暫 ACCESS EXCLUSIVE 可接受；
# PG 11+ 帶 NULL 預設的加欄位本來就不重寫表。

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("knowledge", "0004_embedding_rls"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="uploaded_by",
            field=models.UUIDField(blank=True, null=True),
        ),
    ]

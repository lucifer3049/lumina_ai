# Notification：使用者的通知收件匣（05 §3.3、04 §8.5，2A-5）。普通表（不分區，
# 理由見 model docstring）；建表後立即開 RLS——通知的標題就是內容摘要
# （「法規手冊.pdf 解析失敗」），跨租戶讀得到等於把對方的檔名與失敗原因端出去。

from __future__ import annotations

import uuid
from typing import Any

import django.contrib.postgres.fields
import django.db.models.deletion
from django.db import migrations, models


def _enable_rls(apps: Any, schema_editor: Any) -> None:
    from apps.platform.migrations import _rls

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(_rls.enable("platform_notification"))


def _disable_rls(apps: Any, schema_editor: Any) -> None:
    from apps.platform.migrations import _rls

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(_rls.disable("platform_notification"))


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0010_audit_permission_seed"),
        ("platform", "0005_auditlog"),
    ]

    operations = [
        migrations.CreateModel(
            name="Notification",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("user_id", models.UUIDField()),
                ("type", models.TextField()),
                ("title", models.TextField()),
                ("body", models.TextField(blank=True, default="")),
                (
                    "channels",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.TextField(), default=list, size=None
                    ),
                ),
                ("meta", models.JSONField(blank=True, default=dict)),
                ("dedupe_key", models.TextField(blank=True, null=True)),
                ("read_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="notifications",
                        to="identity.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "platform_notification",
                "indexes": [
                    models.Index(
                        fields=["tenant", "user_id", "-updated_at"], name="ix_notification_inbox"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("dedupe_key__isnull", False)),
                        fields=("tenant", "user_id", "dedupe_key"),
                        name="uq_notification_dedupe",
                    )
                ],
            },
        ),
        migrations.RunPython(_enable_rls, _disable_rls),
    ]

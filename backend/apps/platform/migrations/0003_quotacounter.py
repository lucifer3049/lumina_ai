# QuotaCounter：quota 的對帳快照（05 §3.3，2A-2b）。普通表（不分區，理由見 model
# docstring）；建表後立即開 RLS——快照的內容是用量輪廓，與 usage_logs 同級敏感。

from __future__ import annotations

import uuid
from typing import Any

import django.db.models.deletion
from django.db import migrations, models


def _enable_rls(apps: Any, schema_editor: Any) -> None:
    from apps.platform.migrations import _rls

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(_rls.enable("platform_quotacounter"))


def _disable_rls(apps: Any, schema_editor: Any) -> None:
    from apps.platform.migrations import _rls

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(_rls.disable("platform_quotacounter"))


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0008_chat_permission_seed"),
        ("platform", "0002_usagelog"),
    ]

    operations = [
        migrations.CreateModel(
            name="QuotaCounter",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("resource", models.TextField()),
                ("period", models.TextField()),
                ("period_start", models.DateField()),
                ("used", models.BigIntegerField(default=0)),
                ("limit", models.BigIntegerField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="quota_counters",
                        to="identity.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "platform_quotacounter",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "resource", "period", "period_start"),
                        name="uq_quotacounter_period",
                    )
                ],
            },
        ),
        migrations.RunPython(_enable_rls, _disable_rls),
    ]

# AuditLog：按月分區表 + append-only（05 §3.3、§4、§5.2、04 §8.3，2A-4）。
#
# 分區的模式照抄 `0002_usagelog.py`，不重述；兩處差異：
#
# 1. **append-only 擋在資料庫上**：BEFORE UPDATE OR DELETE 的 row trigger，
#    建在父表即傳播到現有與未來的每一個分區（PG 13+ 的行為，同索引）。
#    Repository 不提供 update 只擋得住走 Repository 的人；稽核要擋的是所有人，
#    包含拿著 `manage.py shell` 的自己人——那正是稽核唯一的用途。
#    保留政策不受影響：到期是 DROP／DETACH 整個分區（DDL），不是 DELETE。
#
# 2. **第二組索引 (tenant_id, resource_type, resource_id)**（05 §4）：稽核的
#    第二種查法是「這份文件被誰動過」，少了它就是掃整個月的分區。
#
# `created_at` 沒有 DEFAULT now()：值一律由應用層給（model 的 `default=timezone.now`）
# ——append-only 的表事後調不了時間，回填舊資料時必須能指定。

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import django.utils.timezone
from django.db import migrations, models

MONTHS_AHEAD = 12


def _create_auditlog() -> str:
    """欄位順序與型別必須與下方 state_operations 的 CreateModel 一致。"""
    return """
        CREATE TABLE platform_auditlog (
            id uuid NOT NULL,
            tenant_id uuid NOT NULL REFERENCES identity_tenant(id) DEFERRABLE INITIALLY DEFERRED,
            actor_id uuid NULL,
            actor_type text NOT NULL,
            action text NOT NULL,
            resource_type text NOT NULL,
            resource_id uuid NULL,
            before jsonb NULL,
            after jsonb NULL,
            outcome text NOT NULL,
            status integer NULL,
            permission text NULL,
            ip inet NULL,
            user_agent text NOT NULL DEFAULT '',
            request_id text NOT NULL,
            created_at timestamptz NOT NULL,
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at);

        -- 05 §4 的兩組索引。建在父表上才會自動傳播到每一個分區。
        CREATE INDEX ix_auditlog_tenant_created
            ON platform_auditlog (tenant_id, created_at);
        CREATE INDEX ix_auditlog_tenant_resource
            ON platform_auditlog (tenant_id, resource_type, resource_id);
    """


def _drop_auditlog() -> str:
    return """
        DROP TABLE IF EXISTS platform_auditlog CASCADE;
        DROP FUNCTION IF EXISTS platform_auditlog_reject_change();
    """


def _create_append_only_guard() -> str:
    """訊息刻意含 `append-only`：出現在錯誤裡的那一行就是說明。"""
    return """
        CREATE OR REPLACE FUNCTION platform_auditlog_reject_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION
                'platform_auditlog is append-only: % is not allowed', TG_OP
                USING HINT = '到期清理請 DROP/DETACH 分區，不要 DELETE 個別列';
        END;
        $$;

        CREATE TRIGGER platform_auditlog_append_only
            BEFORE UPDATE OR DELETE ON platform_auditlog
            FOR EACH ROW EXECUTE FUNCTION platform_auditlog_reject_change();
    """


def _drop_append_only_guard() -> str:
    return """
        DROP TRIGGER IF EXISTS platform_auditlog_append_only ON platform_auditlog;
        DROP FUNCTION IF EXISTS platform_auditlog_reject_change();
    """


def _month_bounds(index: int) -> tuple[str, str, str]:
    """第 `index` 個月的分區名與上下界（0 = migration 執行當月）。"""
    now = datetime.now(UTC)
    year, month = now.year, now.month + index
    year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
    next_year, next_month = (year, month + 1) if month < 12 else (year + 1, 1)
    return (
        f"platform_auditlog_{year:04d}_{month:02d}",
        f"{year:04d}-{month:02d}-01",
        f"{next_year:04d}-{next_month:02d}-01",
    )


def _create_partitions(apps: Any, schema_editor: Any) -> None:
    """預建 `MONTHS_AHEAD` 個月的分區，並逐一開啟 RLS（父表的 policy 管不到
    直接查子分區的人）。"""
    from apps.platform.migrations import _rls

    with schema_editor.connection.cursor() as cursor:
        for index in range(MONTHS_AHEAD):
            name, start, end = _month_bounds(index)
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF platform_auditlog "
                f"FOR VALUES FROM ('{start}') TO ('{end}');"
            )
            cursor.execute(_rls.enable(name))


def _enable_parent_rls(apps: Any, schema_editor: Any) -> None:
    from apps.platform.migrations import _rls

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(_rls.enable("platform_auditlog"))


def _noop(apps: Any, schema_editor: Any) -> None:
    """反向不做事：分區與 policy 隨父表的 DROP TABLE ... CASCADE 一起消失。"""


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0009_analytics_permission_seed"),
        ("platform", "0004_usagedaily"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="AuditLog",
                    fields=[
                        (
                            "id",
                            models.UUIDField(
                                default=uuid.uuid4,
                                editable=False,
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        ("actor_id", models.UUIDField(blank=True, null=True)),
                        ("actor_type", models.TextField()),
                        ("action", models.TextField()),
                        ("resource_type", models.TextField()),
                        ("resource_id", models.UUIDField(blank=True, null=True)),
                        ("before", models.JSONField(blank=True, null=True)),
                        ("after", models.JSONField(blank=True, null=True)),
                        ("outcome", models.TextField()),
                        ("status", models.IntegerField(blank=True, null=True)),
                        ("permission", models.TextField(blank=True, null=True)),
                        ("ip", models.GenericIPAddressField(blank=True, null=True)),
                        ("user_agent", models.TextField(default="")),
                        ("request_id", models.TextField()),
                        ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                        (
                            "tenant",
                            models.ForeignKey(
                                on_delete=models.deletion.PROTECT,
                                related_name="audit_logs",
                                to="identity.tenant",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "platform_auditlog",
                    },
                )
            ],
            database_operations=[
                migrations.RunSQL(sql=_create_auditlog(), reverse_sql=_drop_auditlog()),
                migrations.RunPython(_enable_parent_rls, _noop),
                migrations.RunPython(_create_partitions, _noop),
                migrations.RunSQL(
                    sql=_create_append_only_guard(), reverse_sql=_drop_append_only_guard()
                ),
            ],
        ),
    ]

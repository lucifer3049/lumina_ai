# UsageLog：按月分區表（05 §3.3、§5.2，2A-1）。
#
# 模式照抄 `conversation/migrations/0001_initial.py`（1D-1），不重述整套理由，只記
# 差異：這張表 **append-only**（帳不改、不刪），因此沒有 updated_at／deleted_at；
# 保留政策是「原始 13 個月、彙總永久」（05 §7），到期 DETACH/DROP 整個分區。
#
# 預建 12 個月＋刻意不建 DEFAULT 分區，同 1D-1 的決定；分區用完之前有三道防線：
# 本檔的預建、`tests/integration/test_usage_models.py::test_enough_future_partitions_exist`
# 的提前紅燈、以及 2A-1 起真的存在的 Beat（`platform.maintain_partitions`）。

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from django.db import migrations, models

MONTHS_AHEAD = 12


def _create_usagelog() -> str:
    """欄位順序與型別必須與下方 state_operations 的 CreateModel 一致。

    **PK 是 (id, created_at)**：PostgreSQL 要求分區鍵在每個唯一約束裡。
    """
    return """
        CREATE TABLE platform_usagelog (
            id uuid NOT NULL,
            tenant_id uuid NOT NULL REFERENCES identity_tenant(id) DEFERRABLE INITIALLY DEFERRED,
            user_id uuid NULL,
            category text NOT NULL,
            model text NOT NULL,
            prompt_tokens integer NOT NULL DEFAULT 0,
            completion_tokens integer NOT NULL DEFAULT 0,
            cost numeric(12, 6) NULL,
            conversation_id uuid NULL,
            request_id text NOT NULL,
            created_at timestamptz NOT NULL,
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at);

        -- 05 §4 的兩組索引。建在父表上才會自動傳播到每一個分區（含 Beat 之後新建的）。
        CREATE INDEX ix_usagelog_tenant_created
            ON platform_usagelog (tenant_id, created_at);
        CREATE INDEX ix_usagelog_request
            ON platform_usagelog (request_id);
    """


def _drop_usagelog() -> str:
    return "DROP TABLE IF EXISTS platform_usagelog CASCADE;"


def _month_bounds(index: int) -> tuple[str, str, str]:
    """第 `index` 個月的分區名與上下界（以 migration 執行當月為第 0 個）。"""
    now = datetime.now(UTC)
    year, month = now.year, now.month + index
    year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
    next_year, next_month = (year, month + 1) if month < 12 else (year + 1, 1)
    return (
        f"platform_usagelog_{year:04d}_{month:02d}",
        f"{year:04d}-{month:02d}-01",
        f"{next_year:04d}-{next_month:02d}-01",
    )


def _create_partitions(apps: Any, schema_editor: Any) -> None:
    """預建 `MONTHS_AHEAD` 個月的分區，並逐一開啟 RLS。

    每個分區都要自己開 RLS（父表的 policy 管不到直接查子分區的人）——理由詳
    `conversation/migrations/0001_initial.py` 的同名函式。
    """
    from apps.platform.migrations import _rls

    with schema_editor.connection.cursor() as cursor:
        for index in range(MONTHS_AHEAD):
            name, start, end = _month_bounds(index)
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF platform_usagelog "
                f"FOR VALUES FROM ('{start}') TO ('{end}');"
            )
            cursor.execute(_rls.enable(name))


def _enable_parent_rls(apps: Any, schema_editor: Any) -> None:
    from apps.platform.migrations import _rls

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(_rls.enable("platform_usagelog"))


def _noop(apps: Any, schema_editor: Any) -> None:
    """反向不做事：分區與 policy 隨父表的 DROP TABLE ... CASCADE 一起消失。"""


class Migration(migrations.Migration):
    dependencies = [
        ("platform", "0001_extensions"),
        ("identity", "0008_chat_permission_seed"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="UsageLog",
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
                        ("user_id", models.UUIDField(blank=True, null=True)),
                        ("category", models.TextField()),
                        ("model", models.TextField()),
                        ("prompt_tokens", models.IntegerField(default=0)),
                        ("completion_tokens", models.IntegerField(default=0)),
                        (
                            "cost",
                            models.DecimalField(
                                blank=True, decimal_places=6, max_digits=12, null=True
                            ),
                        ),
                        ("conversation_id", models.UUIDField(blank=True, null=True)),
                        ("request_id", models.TextField()),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        (
                            "tenant",
                            models.ForeignKey(
                                on_delete=models.deletion.PROTECT,
                                related_name="usage_logs",
                                to="identity.tenant",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "platform_usagelog",
                    },
                )
            ],
            database_operations=[
                migrations.RunSQL(sql=_create_usagelog(), reverse_sql=_drop_usagelog()),
                migrations.RunPython(_enable_parent_rls, _noop),
                migrations.RunPython(_create_partitions, _noop),
            ],
        ),
    ]

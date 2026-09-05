# UsageLog 的分區往回補一個月（2026-09-05 修）。
#
# `0002_usagelog` 只從「migration 執行當月」往後預建 12 個月，於是**剛建好的資料庫
# 收不下上個月的列**。而這張表是收得下的——`UsageLog.created_at` 刻意不用
# `auto_now_add`（見 `apps/platform/models.py` 的註解）：append-only 的表事後調不了
# 時間，值只能在 INSERT 當下給，回填舊資料與測試都需要。
#
# 症狀是**日曆觸發**的：任何寫入 N 天前的動作，在每個月的頭 N 天會撞上
#   django.db.utils.IntegrityError: no partition of relation "platform_usagelog" found for row
# 而其餘日子完全正常。2026-09-05 就是這樣紅的（`tests/api/test_analytics_endpoints.py`
# 的 `days_ago=5` 落在 08-31），**前一天跑同一份程式碼是綠的**——最容易被當成 flake
# 忽略掉的那種紅，而 CI 每個月都會中一次。
#
# **為什麼是新的一支而不是改 0002**：0002 已經套用過，Django 不會重跑它，改了對現有
# 資料庫沒有任何效果；而新的一支對新舊資料庫都成立（既有的那個月份已存在，
# `IF NOT EXISTS` 直接略過）。理由同 2B-6 的 0008 與 2C-2 的 RLS 那一支。
#
# **只往回一個月**：時鐘偏移、跨午夜才落地的 task、以及測試的回填都在這個範圍內。
# 更早的歷史匯入是另一回事——那種操作應該自己備妥分區，而不是讓每個新資料庫都先長
# 出十三個空分區。守門在 `tests/integration/test_usage_models.py::test_last_month_is_covered_too`。
#
# 未來的分區不必在這裡管：Beat 的 `platform.maintain_partitions` 每月補到未來 3 個月，
# 而「上個月」在它跑到的時候本來就已經存在（它曾經是當月）。缺的只有**剛建好**的那一刻。

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from django.db import migrations

MONTHS_BACK = 1


def _month_bounds(index: int) -> tuple[str, str, str]:
    """第 ``index`` 個月的分區名與上下界（以本次執行當月為第 0 個，可為負）。

    與 `0002_usagelog._month_bounds` 是同一段邏輯，**刻意複製而不是 import**：
    migration 是歷史快照，跨檔共用會讓改一支影響另一支已經發生過的行為；何況模組名
    以數字開頭，正常的 import 語法根本寫不出來。

    負的 ``index`` 靠 Python 的向下取整除法自然成立：一月的前一個月是去年十二月
    （`month=0` → `(0-1)//12 == -1` 退一年、`(0-1)%12+1 == 12`）。
    """
    now = datetime.now(UTC)
    year, month = now.year, now.month + index
    year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
    next_year, next_month = (year, month + 1) if month < 12 else (year + 1, 1)
    return (
        f"platform_usagelog_{year:04d}_{month:02d}",
        f"{year:04d}-{month:02d}-01",
        f"{next_year:04d}-{next_month:02d}-01",
    )


def _create_past_partitions(apps: Any, schema_editor: Any) -> None:
    """補上過去 `MONTHS_BACK` 個月的分區，並逐一開啟 RLS。

    每個分區都要自己開 RLS——父表的 policy 管不到直接查子分區的人（理由詳
    `apps/platform/migrations/_rls.py`）。漏開的症狀是「某幾個月份的帳沒有隔離」，
    而那不會有任何錯誤訊息。
    """
    from apps.platform.migrations import _rls

    with schema_editor.connection.cursor() as cursor:
        for index in range(-MONTHS_BACK, 0):
            name, start, end = _month_bounds(index)
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF platform_usagelog "
                f"FOR VALUES FROM ('{start}') TO ('{end}');"
            )
            cursor.execute(_rls.enable(name))


def _noop(apps: Any, schema_editor: Any) -> None:
    """反向不做事：分區隨父表的 DROP TABLE ... CASCADE 一起消失（同 0002）。"""


class Migration(migrations.Migration):
    dependencies = [
        ("platform", "0008_credentials_rls"),
    ]

    operations = [
        migrations.RunPython(_create_past_partitions, _noop),
    ]

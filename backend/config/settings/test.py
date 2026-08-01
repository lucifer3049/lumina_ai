"""測試設定（A 組正確性測試）。

與 dev 的唯一差異：**直連 PostgreSQL(15432)，繞過 PgBouncer**。
原因：pytest-django 需要 CREATE/DROP DATABASE，而 PgBouncer transaction mode
綁定固定 dbname，無法代理這類操作。

`CONN_MAX_AGE` 在測試中固定為 0：測試要的是可預期的連線行為，不是效能。
"""

from __future__ import annotations

import os

from .base import *  # noqa: F403
from .base import DATABASES

DATABASES["default"]["HOST"] = os.environ.get("DB_DIRECT_HOST", "127.0.0.1")
DATABASES["default"]["PORT"] = os.environ.get("DB_DIRECT_PORT", "15432")
DATABASES["default"]["CONN_MAX_AGE"] = 0

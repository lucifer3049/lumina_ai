"""Django settings。

ADR-001：Django 在本專案只是 **ORM + Migration 引擎**，不對外提供 HTTP。
所以此檔沒有 ROOT_URLCONF、沒有 MIDDLEWARE、沒有 TEMPLATES——那些是 FastAPI
的職責。只設定「讓 ORM 跑起來」的最小集合。

**橋接的兩個旋鈕**（全部走環境變數，改設定不必改碼）：

- ``CONN_MAX_AGE``（秒）：連線重用時間。``0`` = 每次 ORM 呼叫重建連線。
  05 §5.5 定為 ``300``，依據是 spike B2 實測（單改此項吞吐 2.4 倍）。
- ``ORM_THREADPOOL_SIZE``：``sync_to_async`` 用的 threadpool 大小。
  11 §1.3 已將此列為**次要**旋鈕（實測 12/24/48 無顯著差異）。

**statement_timeout 為什麼不在這個檔案裡**（CLAUDE.md：對外呼叫必有 timeout）：

    直覺寫法是 ``OPTIONS["options"] = "-c statement_timeout=30s"``，但那是
    **startup 參數**，而 docker/pgbouncer/pgbouncer.ini 的
    ``ignore_startup_parameters`` 含 ``options``——PgBouncer 會把它靜默丟棄，
    設定看起來有寫、實際完全沒生效。也不能改用 ``SET``：transaction pooling
    下 session 級設定不會跟著 client 連線走（見 repositories/base.py）。

    因此 statement_timeout 設在 **DB 端的 role 上**，由 ``make db-timeouts``
    套用（冪等，``make up`` 會自動帶，新舊資料卷都適用）。**只套應用角色**：
    套到 migration 角色會砍掉大表的 ``AddIndexConcurrently`` 與 HNSW 建索引，
    而那時 schema 已經是半套的（13 §3.1 的 1A-P3）。

    值的來源是 **11 §4.1 Timeout 全域字典的「DB 5s」**，宣告在下方
    :data:`DB_STATEMENT_TIMEOUT`。Makefile 有一份同名變數餵給 psql；兩者漂移
    時 ``tests/test_db_timeouts.py`` 會失敗——它比對的是 DB 上**實際生效**的值。
"""

from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _required_env(name: str) -> str:
    """缺值即拒絕啟動（鐵則 9 / Fail Fast）。

    有預設值的憑證比沒有更危險：設定漏帶時程式照常起來，只是連到別的地方，
    或用一把「大家都知道」的金鑰簽東西。所以這裡不給 fallback。
    """
    value = os.environ.get(name)
    if not value:
        raise ImproperlyConfigured(f"缺少環境變數 {name}——複製 .env.example 為 .env 後填值")
    return value


SECRET_KEY = _required_env("DJANGO_SECRET_KEY")
DEBUG = False
ALLOWED_HOSTS: list[str] = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    # extension 的 migration 落腳處（apps/platform/migrations/0001_extensions.py）；
    # 表定義隨 Phase 2 的 2A 工作包進來。
    "apps.platform",
    "apps.identity",
    "apps.knowledge",
    "apps.conversation",
    "apps.ai",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "lumina"),
        # 應用角色：非 superuser、非 owner、非 BYPASSRLS（05 §5.1、13 §3.1）。
        # RLS 的四條豁免路徑都繞過 policy 且完全無症狀，所以「應用連線用哪個角色」
        # 不是設定偏好而是隔離機制本身——驗收在 tests/integration/test_db_roles.py。
        "USER": os.environ.get("DB_USER", "lumina_app"),
        "PASSWORD": _required_env("DB_PASSWORD"),
        "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
        # 預設連 PgBouncer(16432)，不直連 PG(15432)——見 docker/compose.yml
        "PORT": os.environ.get("DB_PORT", "16432"),
        "CONN_MAX_AGE": int(os.environ.get("CONN_MAX_AGE", "300")),
        # 重用連線時必開：連線可能已被對端關閉，健康檢查避免拿到死連線。
        "CONN_HEALTH_CHECKS": True,
        # PgBouncer transaction mode 與 server-side cursor 不相容，而 Django 的
        # 預設是**開啟**。`QuerySet.iterator()` 會 `DECLARE` 一個 cursor 再分批
        # `FETCH`，兩者落在不同交易 → 不同的 server 連線 → `InvalidCursorName:
        # cursor "_django_curs_..." does not exist`。
        #
        # 現在就關掉而不是等踩到：iterator() 是 ETL / 匯出處理大量列的標準寫法
        # （Phase 2 的 2x 工作包必然會用），而 config/settings/test.py 直連 PG
        # 繞過 PgBouncer——測試永遠是綠的，只有部署環境會炸。
        # 關閉後 iterator() 退化成 client 端分批，語意不變。
        "DISABLE_SERVER_SIDE_CURSORS": True,
        "OPTIONS": {
            # PgBouncer transaction mode 不支援 server-side prepared statement
            # （05 §5.5）。psycopg3 預設會 prepare，不關掉會直接報錯。
            "prepare_threshold": None,
            # CLAUDE.md：所有對外呼叫必有 timeout。
            # connect_timeout 是 libpq 連線建立階段的參數，不是 startup 參數，
            # PgBouncer 不會攔——連不上時 5 秒放棄，不讓 threadpool 執行緒卡死。
            # 11 §4.1 Timeout 全域字典：DB 5s。
            "connect_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT", "5")),
        },
    },
    # ── migration / 維運連線（13 §3.1 的 1A-P2）───────────────────
    # schema owner 角色，只在三個地方使用：`make migrate`、pytest 建立 test
    # database、維運腳本。應用執行期永遠不碰它。
    #
    # 為什麼不共用 default：pytest 需要 CREATE DATABASE，而該權限給了應用角色
    # 就等於讓它能自建一個不受任何 policy 保護的資料庫；反過來，若整個測試套件
    # 都以 owner 跑，1A-2 的跨租戶矩陣會在 RLS 完全失效時全綠——那比沒有測試更
    # 糟，它會主動背書一個不存在的保護。
    #
    # 為什麼直連 PostgreSQL 而非經 PgBouncer：transaction pooling 下
    # `CREATE DATABASE` 不可行（連線池綁定固定 dbname），migration 取的 advisory
    # lock 與 `CREATE INDEX CONCURRENTLY` 語意也會壞掉（05 §5.5）。
    "admin": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "lumina"),
        "USER": os.environ.get("DB_ADMIN_USER", "lumina_owner"),
        "PASSWORD": _required_env("DB_ADMIN_PASSWORD"),
        "HOST": os.environ.get("DB_DIRECT_HOST", "127.0.0.1"),
        "PORT": os.environ.get("DB_DIRECT_PORT", "15432"),
        # 不重用：這條連線只在 migration / 建庫時短暫使用，長連線會一直佔著
        # 一條特權連線，而它的權限遠大於應用連線。
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": True,
        "DISABLE_SERVER_SIDE_CURSORS": True,
        "OPTIONS": {
            # 直連 PG 其實可以 prepare，但兩條連線的行為差異會讓「migration 環境
            # 能跑、應用環境不能」這類問題更難查，所以維持一致。
            "prepare_threshold": None,
            "connect_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT", "5")),
        },
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"

# 11 §4.1 Timeout 全域字典：DB 5s。這是 role 層級設定，由 make db-timeouts 套用；
# 本常數是「應該是多少」的宣告，供 tests/test_db_timeouts.py 與 DB 實際值比對。
DB_STATEMENT_TIMEOUT = os.environ.get("DB_STATEMENT_TIMEOUT", "5s")

# 日誌設定不走 Django（12 §1.1）：唯一入口是 config/logging.py 的 configure_logging()，
# 由 config/asgi.py 與 manage.py 呼叫。留著 Django 的 dictConfig 會有兩套設定互相覆蓋，
# 而「哪一份生效」得靠讀原始碼推理——真正的症狀是 log 格式時好時壞。
LOGGING_CONFIG = None

# ADR-001 threadpool 大小；預設 2×CPU（11 §1.3 起步值）
ORM_THREADPOOL_SIZE = int(os.environ.get("ORM_THREADPOOL_SIZE", str((os.cpu_count() or 4) * 2)))

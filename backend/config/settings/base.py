"""Django settings —— spike 範圍（ADR-001 橋接驗證）。

ADR-001：Django 在本專案只是 **ORM + Migration 引擎**，不對外提供 HTTP。
所以此檔沒有 ROOT_URLCONF、沒有 MIDDLEWARE、沒有 TEMPLATES——那些是 FastAPI
的職責。只設定「讓 ORM 跑起來」的最小集合。

**B 組壓測的兩個旋鈕**（全部走環境變數，改設定不必改碼）：

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
    套用（冪等，``make up`` 會自動帶，新舊資料卷都適用）。

    值的來源是 **11 §4.1 Timeout 全域字典的「DB 5s」**，宣告在下方
    :data:`DB_STATEMENT_TIMEOUT`。Makefile 有一份同名變數餵給 psql；兩者漂移
    時 ``tests/test_db_timeouts.py`` 會失敗——它比對的是 DB 上**實際生效**的值。
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# spike 專用：Django 在此不簽 session/cookie，SECRET_KEY 無實質作用。
# Phase 0 起改為必填環境變數（缺值即拒絕啟動）。
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "spike-only-not-a-secret")
DEBUG = False
ALLOWED_HOSTS: list[str] = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "apps.spike",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "lumina"),
        "USER": os.environ.get("DB_USER", "lumina"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "lumina_spike_pw"),
        "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
        # 預設連 PgBouncer(16432)，不直連 PG(15432)——見 docker/compose.spike.yml
        "PORT": os.environ.get("DB_PORT", "16432"),
        "CONN_MAX_AGE": int(os.environ.get("CONN_MAX_AGE", "300")),
        # 重用連線時必開：連線可能已被對端關閉，健康檢查避免拿到死連線。
        "CONN_HEALTH_CHECKS": True,
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
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"

# 11 §4.1 Timeout 全域字典：DB 5s。這是 role 層級設定，由 make db-timeouts 套用；
# 本常數是「應該是多少」的宣告，供 tests/test_db_timeouts.py 與 DB 實際值比對。
DB_STATEMENT_TIMEOUT = os.environ.get("DB_STATEMENT_TIMEOUT", "5s")

# ADR-001 threadpool 大小；預設 2×CPU（11 §1.3 起步值）
ORM_THREADPOOL_SIZE = int(os.environ.get("ORM_THREADPOOL_SIZE", str((os.cpu_count() or 4) * 2)))

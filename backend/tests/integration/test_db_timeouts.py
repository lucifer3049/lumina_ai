"""規格值對帳 —— DB timeout 是否真的等於 11 §4.1 全域字典的值。

**為什麼要有這個檔案**：`statement_timeout` 的值必然存在兩份（Makefile 餵給
psql 的那份、Django settings 宣告的那份），因為 SQL 與 Python 之間沒有共用
常數的機制。兩份會漂——而漂了不會有任何徵兆，設定看起來都在、只是不一致。

這裡不比對「兩份文字是否相同」（那只證明抄對了），而是查 **DB 上實際生效的
值**，再跟 settings 宣告的值比。改了 Makefile 忘了改 settings、或跑了 psql
卻沒套用成功，都會在這裡紅燈。
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.db import connection


@pytest.mark.django_db
def test_statement_timeout_matches_spec() -> None:
    """DB 實際生效的 statement_timeout == settings.DB_STATEMENT_TIMEOUT。

    失敗時多半是沒跑過 ``make db-timeouts``（`make up` 會自動帶）。
    """
    # **查 role 上的設定，不是當前 session 的生效值**（1D-2 改）。
    #
    # 測試連線刻意用 startup 參數放寬 statement_timeout（見 config/settings/test.py：
    # TRUNCATE 分區表會超過 5 秒），所以 `SHOW statement_timeout` 在測試裡看到的是那個
    # 放寬值，不是 `make db-timeouts` 設進去的。
    #
    # 而這條測試真正要驗的本來就是後者——「ALTER ROLE 有沒有跑、值跟 settings 合不合」
    # ——那是持久設定，查 `pg_roles.rolconfig` 才問得到，且不受任何 session 覆寫影響。
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolconfig FROM pg_roles WHERE rolname = %s",
            [str(settings.DATABASES["default"]["USER"])],
        )
        row = cursor.fetchone()

    assert row is not None, "查不到應用角色——role 拆分（13 §3.1 的 1A-P1）沒跑？"
    configured = dict(
        entry.split("=", 1) for entry in (row[0] or []) if entry.startswith("statement_timeout=")
    )
    effective = configured.get("statement_timeout")
    assert effective == settings.DB_STATEMENT_TIMEOUT, (
        f"role 上設定的是 {effective}，settings 宣告的是 {settings.DB_STATEMENT_TIMEOUT}"
        "——跑 `make db-timeouts` 或修正兩邊的值（規格出自 11 §4.1：DB 5s）"
    )


def test_connect_timeout_is_declared() -> None:
    """連線建立階段也必須有 timeout（11 §4.1），且不是 0/None。"""
    options = settings.DATABASES["default"]["OPTIONS"]
    assert isinstance(options, dict)
    assert options.get("connect_timeout"), "DATABASES OPTIONS 缺 connect_timeout"


# 正式值是 5s（11 §4.1）。測試至少要這個數字才擋得住併發 TRUNCATE 的尖峰；
# 訂成下限而不是等值，是為了讓 config/settings/test.py 之後調大不必回來改測試。
_MIN_TEST_TIMEOUT_MS = 30_000


@pytest.mark.django_db
def test_test_connections_widen_the_statement_timeout() -> None:
    """測試連線的 statement_timeout 必須比正式值寬（1D-2）。

    **這條守的是一個已經發生過的不穩定**：`transaction=True` 的測試在每條結束後
    `TRUNCATE` 全部的表，而 `conversation_message` 是分區表——那一次要鎖父表加 12 個
    分區。六個 xdist worker 同時做時偶爾超過正式值的 5 秒，flush 失敗，上一條測試的
    資料留在庫裡，於是**下一條**測試撞 `duplicate key ... identity_tenant_pkey`。

    受害者是隨機的、單獨跑都會過，所以查起來非常花時間（實測六次全套跑才定位到）。
    把覆寫拿掉會讓它回來，而回來的樣子不會指向這裡——因此需要一條直接盯著它的測試。
    """
    # 比毫秒而不是比字串：PostgreSQL 會把 `60s` 正規化成 `1min`，`SHOW` 回的是
    # 正規化後的樣子。`pg_settings.setting` 對 statement_timeout 一律是毫秒，
    # 不受寫法影響。
    with connection.cursor() as cursor:
        cursor.execute("SELECT setting FROM pg_settings WHERE name = 'statement_timeout'")
        row = cursor.fetchone()

    assert row is not None
    effective_ms = int(row[0])
    assert effective_ms >= _MIN_TEST_TIMEOUT_MS, (
        f"測試連線的 statement_timeout 只有 {effective_ms}ms，"
        f"至少要 {_MIN_TEST_TIMEOUT_MS}ms——TRUNCATE 分區表會偶發超時，"
        "而症狀是別的測試隨機撞唯一鍵（見本函式 docstring）"
    )

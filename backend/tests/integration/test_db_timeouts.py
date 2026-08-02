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
    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        row = cursor.fetchone()

    assert row is not None
    effective = row[0]
    assert effective == settings.DB_STATEMENT_TIMEOUT, (
        f"DB 上生效的是 {effective}，settings 宣告的是 {settings.DB_STATEMENT_TIMEOUT}"
        "——跑 `make db-timeouts` 或修正兩邊的值（規格出自 11 §4.1：DB 5s）"
    )


def test_connect_timeout_is_declared() -> None:
    """連線建立階段也必須有 timeout（11 §4.1），且不是 0/None。"""
    options = settings.DATABASES["default"]["OPTIONS"]
    assert isinstance(options, dict)
    assert options.get("connect_timeout"), "DATABASES OPTIONS 缺 connect_timeout"

"""驗收：診斷用的旋鈕回報不得夾帶基礎設施拓撲（鐵則 9、10 §5）。

``core.db.orm_runtime_knobs()`` 是 ``/spike/healthz`` 的**全部**內容，也是 1A 要建
正式 ``/healthz``（11 §4.2）時的雛形——所以邊界訂在這一層而不是那支端點上：
spike 面會在 1A 整組刪除，這條守門必須活得比它久。

前一版由 controller 自行組回應並回報 ``db_host`` / ``db_port``。診斷端點會長大是
常態（「順便回報一下連得上哪台」永遠有人想加），而這支端點無認證，加了就是把內部
拓撲公開，且不會有任何徵兆。因此用**精確的鍵集合**斷言而不是「不含某些鍵」：
後者對新增的欄位永遠是綠的。
"""

from __future__ import annotations

from django.conf import settings

from core.db import orm_runtime_knobs

# 11 §1.3 / 05 §5.5 的兩個 B 組旋鈕，也是這支端點唯一該回報的東西。
EXPECTED_KEYS = {"conn_max_age", "orm_threadpool_size"}


def test_reports_exactly_the_measurement_knobs() -> None:
    assert set(orm_runtime_knobs()) == EXPECTED_KEYS, (
        "旋鈕回報的鍵集合變了——新增欄位前先確認它不是內部資訊（本端點無認證）"
    )


def test_knob_values_match_the_active_settings() -> None:
    """回報的必須是設定的值本身，不是寫死的數字。

    寫死的話這支端點就從「確認設定生效」變成「複述一個常數」——而它存在的唯一
    理由是上一輪量測「設定沒生效卻照樣記數據」的教訓。
    """
    knobs = orm_runtime_knobs()

    assert knobs["conn_max_age"] == settings.DATABASES["default"]["CONN_MAX_AGE"]
    assert knobs["orm_threadpool_size"] == int(settings.ORM_THREADPOOL_SIZE)


def test_does_not_leak_db_topology() -> None:
    """明確釘住主機與埠不在裡面——這是前一版真正洩漏出去的兩個值。"""
    values = {str(value) for value in orm_runtime_knobs().values()}
    database = settings.DATABASES["default"]

    assert str(database["HOST"]) not in values, "DB 主機洩漏到未認證的診斷端點"
    assert str(database["PORT"]) not in values, "DB 埠洩漏到未認證的診斷端點"

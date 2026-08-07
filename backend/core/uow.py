"""Unit of Work —— 交易邊界 ＋ RLS 的租戶參數（02 §core、05 §5.1、11 §4.3）。

**交易邊界 = Service 方法**（11 §4.3）。Service 用本模組把一次 Use Case 的所有
寫入包成一個交易；跨聚合的一致性走 Domain Event，不靠長交易。

兩件事綁在同一個 context manager 裡，是因為它們必須同進同出：

1. ``transaction.atomic()``——要嘛全成功要嘛全回滾。
2. ``app.tenant_id`` 交易區域參數——PostgreSQL RLS policy 讀的就是它
   （05 §5.1：``USING (tenant_id = current_setting('app.tenant_id')::uuid)``）。
   分開設定的話會出現「有交易但沒設租戶」的窗口，那正是 RLS 失效的形狀。

RLS policy 本身屬 Phase 1（13 §3 工作包 1A）。在 policy 建立之前這個參數沒有
讀者，但**現在就設對**的代價是零、事後補的代價是要回頭審查每一條交易路徑。

## 為什麼是 set_config 而不是 SET LOCAL

PostgreSQL 的 ``SET`` 不吃查詢參數（值必須是字面量），所以 ``SET LOCAL
app.tenant_id = %s`` 是行不通的；改成字串拼接就把使用者可控的值直接拼進 SQL。
``SELECT set_config('app.tenant_id', %s, true)`` 是同義的函式形式，可參數化，
第三個引數 ``true`` 就是 ``LOCAL``（交易結束即失效）。

**必須是交易區域（local）而非 session 級**：PgBouncer 走 transaction pooling
（05 §5.5），連線在交易結束後會還給池子；session 級設定會跟著連線交給下一個
client，變成租戶 A 的值成為租戶 B 的預設——RLS 上線後即為跨租戶讀取。
由 ``tests/integration/test_uow.py::TestTenantGuc::test_guc_does_not_leak_after_transaction``
釘住。

## 只能在 threadpool 執行緒上使用

Django ORM 是同步的（ADR-001）。本模組必須經 :func:`core.db.run_orm` 在
threadpool 內呼叫，不得在 event loop 上直接用——後者會阻塞整個 loop，而症狀是
「高併發下延遲暴增」而不是錯誤。Django 的 ``async_unsafe`` 會在 async context
下丟 ``SynchronousOnlyOperation``，本模組刻意不去接住它。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import connection, transaction

from core.exceptions import CrossTenantTransactionError
from core.tenant import get_current_tenant_id

TENANT_GUC = "app.tenant_id"

# 當前交易已綁定的租戶。巢狀 UoW 靠它偵測「同一交易內換租戶」——見
# unit_of_work 的「為什麼巢狀換租戶必須 raise」。
_active_uow_tenant: ContextVar[uuid.UUID | None] = ContextVar("active_uow_tenant", default=None)

_SET_TENANT_GUC = f"SELECT set_config('{TENANT_GUC}', %s, true)"


@contextmanager
def unit_of_work() -> Iterator[None]:
    """開啟交易並綁定當前租戶。

    巢狀呼叫沿用外層交易（Django ``atomic`` 以 savepoint 實作巢狀），因此
    「Service 呼叫 Service」不會讓內層自成一個交易而在外層失敗時留下半套資料。
    巢狀時會重設一次租戶參數——值相同、成本是一次 ``SELECT``，換取的是「不論從
    哪一層進來，交易內一定有租戶」這個不需要推理的保證。

    缺 TenantContext 一律 raise（鐵則 4）。**不提供無租戶模式**：靜默略過的話
    交易照跑而參數是空的，RLS policy 上線後查詢會回空集合或全表，兩者都不會有
    錯誤訊息。Migration 與維運腳本走 bypass 角色，不經本模組（05 §5.1）。

    ## 為什麼巢狀換租戶必須 raise

    ``set_config(..., true)`` 是交易區域而**不是** savepoint 區域的：在子交易
    （savepoint）內設定的值，只要該子交易正常結束就會**保留到外層交易結束**，
    只有 savepoint 回滾才會還原。所以「外層租戶 A 的交易裡，巢狀進一個租戶 B
    的 UoW」在內層離開後不會回到 A——GUC 停在 B，而外層後續的每一條語句都會在
    RLS 之下被當成 B。ORM 的 tenant filter 仍說 A、DB 的 policy 說 B，兩者不一致
    卻沒有任何錯誤訊息。

    因此偵測到就 raise（:class:`~core.exceptions.CrossTenantTransactionError`）。
    不採「離開時還原成外層值」的修法：那讓「一個交易兩個租戶」變成可用寫法，
    而它在 RLS 之下沒有正確語意——跨租戶操作本來就該各自開交易。
    """
    tenant_id = get_current_tenant_id(operation="unit_of_work")
    active = _active_uow_tenant.get()
    if active is not None and active != tenant_id:
        raise CrossTenantTransactionError(active=str(active), requested=str(tenant_id))

    token = _active_uow_tenant.set(tenant_id)
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                # 參數化：租戶 id 雖然來自 contextvars 而非請求，但這條 SQL 的安全性
                # 不該建立在「呼叫端保證乾淨」上（10 §injection）。
                cursor.execute(_SET_TENANT_GUC, [str(tenant_id)])
            yield
    finally:
        _active_uow_tenant.reset(token)

"""A 組正確性測試 —— Unit of Work（02 §core、05 §5.1、11 §4.3）。

UoW 是**交易邊界**（11 §4.3：交易邊界 = Service 方法）＋ **RLS 的前置設定**
（05 §5.1：交易開始 ``SET LOCAL app.tenant_id``）。

RLS policy 本身屬 Phase 1（13 §3 工作包 1A「隔離機制全量：filter+RLS」），
本檔因此**不驗 policy 行為**，只驗 policy 建立後會依賴的那個交易區域參數有
正確設定與正確作用域。B2 是本檔最關鍵的一條：它在 policy 還不存在的現在，
就能擋住「用了 session 級 ``SET`` 而非 ``SET LOCAL``」——那個錯誤在 PgBouncer
transaction mode 下會讓租戶 A 的設定殘留給下一個借到同一條連線的租戶。

全檔用 ``transactional_db`` 而非 ``db``：pytest-django 的 ``db`` 把整個測試包在
一個交易裡，而本檔驗的正是 commit / rollback 本身——包在外層交易裡會看不到
真實行為（提交被外層吞掉、回滾變成 savepoint 回滾）。
"""

from __future__ import annotations

import uuid

import pytest
from django.core.exceptions import SynchronousOnlyOperation
from django.db import connection

from apps.spike.models import SpikeItem
from core.exceptions import TenantContextMissingError
from core.tenant import tenant_context
from core.uow import unit_of_work
from tests.conftest import TENANT_A

pytestmark = pytest.mark.django_db(transaction=True)


class _BoomError(RuntimeError):
    """測試用例外——不用內建型別，避免與框架自己丟的例外混淆。"""


def _current_setting() -> str | None:
    """讀交易區域參數；**未設定一律正規化成 None**。

    未設定有兩種表現，兩種都算「沒設定」：

    - ``NULL``——這個 session 從未碰過這個自訂參數（``missing_ok=true`` 的回傳）。
    - ``''``——碰過了，但 ``SET LOCAL`` 已隨交易結束還原。PostgreSQL 對帶點號的
      自訂 GUC，一旦在 session 內被設定過就會保留該名稱，交易結束後還原成 session
      層級的值；而 session 層級從未設定，於是是空字串而非 NULL。

    正規化的理由：本檔要斷言的是「值不再是那個租戶」，不是「回傳哪一種空」。
    直接比對 ``is None`` 會在第二種情況假紅燈（實測如此）。
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.tenant_id', true)")
        row = cursor.fetchone()
    value = None if row is None else row[0]
    return value or None


def _titles_of(tenant_id: uuid.UUID) -> set[str]:
    """繞過 repository 直接查——本檔要驗的是交易，不是 tenant filter。"""
    return set(SpikeItem.objects.filter(tenant_id=tenant_id).values_list("title", flat=True))


class TestTransactionBoundary:
    """A 組：UoW 必須是真的交易邊界，不是只把例外吞掉。"""

    def test_rollback_on_exception(self) -> None:
        """A1：區塊內 raise → 該區塊的所有寫入都不存在。"""
        with pytest.raises(_BoomError), tenant_context(TENANT_A), unit_of_work():
            SpikeItem.objects.create(tenant_id=TENANT_A, title="uow-rollback-1")
            SpikeItem.objects.create(tenant_id=TENANT_A, title="uow-rollback-2")
            raise _BoomError

        assert _titles_of(TENANT_A) == set(), "例外後仍有資料 → 沒有真的回滾"

    def test_commit_on_success(self) -> None:
        """A2：正常離開 → 寫入可見。"""
        with tenant_context(TENANT_A), unit_of_work():
            SpikeItem.objects.create(tenant_id=TENANT_A, title="uow-commit")

        assert _titles_of(TENANT_A) == {"uow-commit"}

    def test_nested_uow_shares_one_transaction(self) -> None:
        """A3：巢狀 UoW 不得各自獨立提交。

        「一個 Service 方法呼叫另一個」是必然會出現的形狀。若內層自成一個交易，
        外層失敗時內層已經提交 → 半套資料，而且沒有任何錯誤訊息。
        """
        with pytest.raises(_BoomError), tenant_context(TENANT_A), unit_of_work():
            SpikeItem.objects.create(tenant_id=TENANT_A, title="outer")
            with unit_of_work():
                SpikeItem.objects.create(tenant_id=TENANT_A, title="inner")
            raise _BoomError

        assert _titles_of(TENANT_A) == set(), "內層已獨立提交 → 巢狀交易語意錯誤"


class TestTenantGuc:
    """B 組：RLS（Phase 1）會讀的交易區域參數，現在就要設對。"""

    def test_guc_is_set_inside_transaction(self) -> None:
        """B1：交易內的 ``app.tenant_id`` == 當前 TenantContext。"""
        with tenant_context(TENANT_A), unit_of_work():
            assert _current_setting() == str(TENANT_A)

    def test_guc_does_not_leak_after_transaction(self) -> None:
        """B2：交易結束後設定必須消失（``SET LOCAL`` 而非 ``SET``）。

        **本檔最重要的一條。** PgBouncer 走 transaction pooling，連線在交易結束後
        會還給池子給別的 client 用；session 級的 ``SET`` 會跟著連線一起被交出去，
        於是租戶 A 的設定變成租戶 B 的預設值——RLS 上線後這是跨租戶讀取。
        """
        with tenant_context(TENANT_A), unit_of_work():
            assert _current_setting() == str(TENANT_A)

        assert _current_setting() is None, "設定外洩到交易外 → 用成 SET 而非 SET LOCAL"

    def test_each_tenant_gets_its_own_value(self, two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        """B3：不同租戶的交易各自拿到自己的值，不互相污染。"""
        tenant_a, tenant_b = two_tenants

        with tenant_context(tenant_a), unit_of_work():
            assert _current_setting() == str(tenant_a)
        with tenant_context(tenant_b), unit_of_work():
            assert _current_setting() == str(tenant_b)


class TestFailFast:
    """C 組：缺 TenantContext 不得有退路（鐵則 4）。"""

    def test_raises_without_tenant_context(self) -> None:
        """C1：沒有租戶就進 UoW → raise，而不是靜默略過 SET LOCAL。

        靜默略過的後果是：交易照跑、RLS 參數是空的，policy 上線後查詢會回空集合
        或全表——兩種都不會有錯誤訊息。
        """
        with pytest.raises(TenantContextMissingError), unit_of_work():
            pytest.fail("不該進到區塊內")


class TestAdr001Boundary:
    """D 組：UoW 只能在 threadpool 執行緒上開，不得在 event loop 上。"""

    async def test_refuses_to_open_transaction_on_the_event_loop(self) -> None:
        """D1：從 async context 直接用 UoW → 明確失敗。

        Django ORM 是同步的（ADR-001）：在 event loop 上開交易會阻塞整個 loop，
        而且症狀是「高併發下延遲暴增」而非錯誤。Django 自己的 ``async_unsafe``
        會擋下來——這條測試把那個保證釘住，避免日後有人「順手」把 UoW 包成
        async 而讓保護消失。正確用法是經 ``core.db.run_orm`` 進 threadpool。
        """
        with pytest.raises(SynchronousOnlyOperation), tenant_context(TENANT_A), unit_of_work():
            pytest.fail("不該進到區塊內")

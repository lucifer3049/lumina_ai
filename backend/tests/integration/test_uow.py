"""A 組正確性測試 —— Unit of Work（02 §core、05 §5.1、11 §4.3）。

UoW 是**交易邊界**（11 §4.3：交易邊界 = Service 方法）＋ **RLS 的前置設定**
（05 §5.1：交易開始 ``SET LOCAL app.tenant_id``）。

policy 行為本身由 ``test_rls_identity.py`` 驗；本檔只驗 policy 依賴的那個交易區域
參數有正確設定與正確作用域。B2 是本檔最關鍵的一條：它擋住「用了 session 級
``SET`` 而非 ``SET LOCAL``」——那個錯誤在 PgBouncer transaction mode 下會讓租戶 A
的設定殘留給下一個借到同一條連線的租戶。

全檔用 ``transactional_db`` 而非 ``db``：pytest-django 的 ``db`` 把整個測試包在
一個交易裡，而本檔驗的正是 commit / rollback 本身——包在外層交易裡會看不到
真實行為（提交被外層吞掉、回滾變成 savepoint 回滾）。
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from django.core.exceptions import SynchronousOnlyOperation
from django.db import connection, transaction

from apps.identity.models import User
from core.exceptions import CrossTenantTransactionError, TenantContextMissingError
from core.tenant import tenant_context
from core.uow import unit_of_work
from tests.conftest import TENANT_A, TENANT_B

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


def _make_user(tenant_id: uuid.UUID, email: str) -> None:
    """在**當前交易內**寫一筆使用者。

    繞過 repository 直接用 ORM——本檔要驗的是交易邊界，不是 tenant filter。
    """
    User.objects.create(
        tenant_id=tenant_id,
        email=email,
        display_name=email,
        password_hash="argon2id$placeholder-not-a-real-hash",
    )


def _emails_of(tenant_id: uuid.UUID) -> set[str]:
    """讀該租戶的使用者 email。

    **必須自己開一個 UoW**：`identity_user` 有 RLS，policy 讀的 ``app.tenant_id``
    是交易區域參數。在交易外查會一筆都拿不到——而「一筆都沒有」正是本檔多數斷言
    的期望值，那會讓 test_commit_on_success 之外的每一條都變成假綠燈。
    """
    with tenant_context(tenant_id), unit_of_work():
        return set(User.objects.filter(tenant_id=tenant_id).values_list("email", flat=True))


class TestTransactionBoundary:
    """A 組：UoW 必須是真的交易邊界，不是只把例外吞掉。

    全組依賴 ``two_empty_tenants``：寫入 `identity_user` 需要租戶列先存在（FK），
    但**不能有既有使用者**——本組多數斷言的期望值是「事後一筆都沒有」。
    """

    def test_rollback_on_exception(self, two_empty_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        """A1：區塊內 raise → 該區塊的所有寫入都不存在。"""
        with pytest.raises(_BoomError), tenant_context(TENANT_A), unit_of_work():
            _make_user(TENANT_A, "uow-rollback-1@example.com")
            _make_user(TENANT_A, "uow-rollback-2@example.com")
            raise _BoomError

        assert _emails_of(TENANT_A) == set(), "例外後仍有資料 → 沒有真的回滾"

    def test_commit_on_success(self, two_empty_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        """A2：正常離開 → 寫入可見。"""
        with tenant_context(TENANT_A), unit_of_work():
            _make_user(TENANT_A, "uow-commit@example.com")

        assert _emails_of(TENANT_A) == {"uow-commit@example.com"}

    def test_nested_uow_shares_one_transaction(
        self, two_empty_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """A3：巢狀 UoW 不得各自獨立提交。

        「一個 Service 方法呼叫另一個」是必然會出現的形狀。若內層自成一個交易，
        外層失敗時內層已經提交 → 半套資料，而且沒有任何錯誤訊息。
        """
        with pytest.raises(_BoomError), tenant_context(TENANT_A), unit_of_work():
            _make_user(TENANT_A, "outer@example.com")
            with unit_of_work():
                _make_user(TENANT_A, "inner@example.com")
            raise _BoomError

        assert _emails_of(TENANT_A) == set(), "內層已獨立提交 → 巢狀交易語意錯誤"


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

    def test_each_tenant_gets_its_own_value(
        self, two_empty_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """B3：不同租戶的交易各自拿到自己的值，不互相污染。"""
        tenant_a, tenant_b = two_empty_tenants

        with tenant_context(tenant_a), unit_of_work():
            assert _current_setting() == str(tenant_a)
        with tenant_context(tenant_b), unit_of_work():
            assert _current_setting() == str(tenant_b)


class TestNestedTenantGuard:
    """B 組續：同一個交易內不得出現第二個租戶。

    ``set_config(..., true)`` 是**交易**區域而不是 savepoint 區域的：在子交易
    （Django 巢狀 ``atomic`` = savepoint）內設定的值，只要該子交易正常結束就會
    保留到**外層交易**結束，只有 savepoint 回滾才還原。

    所以「租戶 A 的交易裡巢狀進一個租戶 B 的 UoW」在內層離開後不會回到 A：GUC 停在
    B，而外層後續的每一條語句都會在 RLS 之下被當成 B。ORM 的 tenant filter 說 A、
    DB 的 policy 說 B，兩者不一致卻沒有任何錯誤訊息——RLS 上線後這是跨租戶寫入。
    """

    def test_nested_uow_with_another_tenant_raises(self) -> None:
        """B4：偵測到就 raise，不允許「一個交易兩個租戶」成為可用寫法。"""
        with tenant_context(TENANT_A), unit_of_work():
            assert _current_setting() == str(TENANT_A), "前提不成立：外層交易沒綁到租戶 A"

            with (
                pytest.raises(CrossTenantTransactionError),
                tenant_context(TENANT_B),
                unit_of_work(),
            ):
                pytest.fail("不該進到區塊內")

    def test_outer_tenant_survives_the_rejected_nesting(self) -> None:
        """B5：守門的重點不是「有例外」，是**外層的 GUC 沒有被改掉**。

        這條才是會抓到迴歸的那一條：若日後有人把守門改成「內層離開時還原成外層
        值」，B4 會紅、看起來只是規則變寬；而真正的風險是還原沒做到位，那只有這裡
        看得出來。
        """
        with tenant_context(TENANT_A), unit_of_work():
            with (
                contextlib.suppress(CrossTenantTransactionError),
                tenant_context(TENANT_B),
                unit_of_work(),
            ):
                pass

            assert _current_setting() == str(TENANT_A), "外層交易的租戶參數被內層改掉了"

    def test_guard_is_released_after_the_transaction(self) -> None:
        """B6：守門只在交易內生效——交易各自獨立時換租戶是正常用法。

        守門若靠一個沒有還原的旗標實作，第二個交易會被誤擋，而症狀是「換租戶就
        壞掉」這種完全無關的表象。
        """
        with tenant_context(TENANT_A), unit_of_work():
            pass
        with tenant_context(TENANT_B), unit_of_work():
            assert _current_setting() == str(TENANT_B)

    def test_postgres_keeps_subtransaction_local_settings(self) -> None:
        """B7：釘住守門的**前提**——子交易正常結束時 ``SET LOCAL`` 不會還原。

        驗的是 PostgreSQL 的行為，不是我們的程式碼，所以刻意繞過 UoW 直接下 SQL。
        沒有這一條，上面三條測試的理由就只是註解裡的一句斷言；而這個前提若在某次
        PG 升版後改變，守門的設計依據就要重新評估（雖然 raise 本身仍然合理）。
        """
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(TENANT_A)])

            # Django 巢狀 atomic = savepoint
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(TENANT_B)])

            assert _current_setting() == str(TENANT_B), (
                "PostgreSQL 的行為變了：子交易的 SET LOCAL 現在會隨 savepoint 還原"
                "——core/uow.py 跨租戶守門的依據需要重新評估"
            )


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

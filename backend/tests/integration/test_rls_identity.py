"""驗收：Identity 六張表的 RLS（05 §5.1、13 §3 工作包 1A）。

**這是整個多租戶隔離的第二道防線，也是唯一在程式寫錯時還救得回來的那道。**
第一道是 Repository 的 tenant filter（`repositories/base.py`），它只證明「今天
的程式寫對了」；RLS 證明的是「明天有人漏寫 filter，資料仍然不會外洩」。

因此本檔的查詢**刻意繞過 Repository、直接下沒有 WHERE tenant_id 的原生 SQL**。
用 ORM 查會同時經過兩道防線，綠燈無法分辨是哪一道擋下的——那正是最需要分辨的
時候。

四種寫入路徑分開驗（SELECT / INSERT / UPDATE / DELETE）：policy 的 ``USING``
管的是「看得到哪些列」，``WITH CHECK`` 管的是「寫進去的列長什麼樣」。只寫
``USING`` 的 policy 擋得住讀、擋不住把資料寫進別的租戶名下，而那個漏洞在讀取
測試裡完全看不出來。

全檔 ``transaction=True``：RLS 依賴 ``core/uow.py`` 在交易內設定的
``app.tenant_id``，而 pytest-django 預設的 ``db`` 會把整個測試包進外層交易，
交易邊界的行為會被吃掉。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from django.db import connection, connections
from django.db.utils import ProgrammingError

from core.tenant import tenant_context
from core.uow import unit_of_work
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import (
    make_permission,
    make_system_role,
    make_tenant,
    make_tenant_role,
    make_user,
    tenant_scope,
)
from tests.seed import ensure_identity_seed

# `admin` 也要列進來：全域權限字典的寫入走 owner 連線（`PermissionFactory`——應用角色
# 對 `identity_permission` 只剩 SELECT，見 0012_platform_table_grants）。
pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

# 需要租戶隔離的 identity 表。`permissions` 不在其中——它是全域字典表，
# 理由見 test_permissions_is_intentionally_global。
TENANT_SCOPED_IDENTITY_TABLES = (
    "identity_tenant",
    "identity_user",
    "identity_role",
    "identity_user_role",
    "identity_role_permission",
    # 1A 只建表、不接判定邏輯（13 §4 把資源級 grant 延後），但表既然存在就要有
    # policy——補在有資料之後的成本高得多。
    "identity_resource_grant",
)


@pytest.fixture
def two_tenants_with_users() -> Iterator[dict[str, uuid.UUID]]:
    """兩個租戶各一個使用者，**email 故意相同**。

    相同 email 是刻意的：它同時驗兩件事——`UNIQUE(tenant_id, email)` 是租戶內
    唯一而非全域唯一（不同公司當然可能有同名信箱），以及跨租戶查詢時不會因為
    email 相同就撈錯人。
    """
    shared_email = "same@example.com"

    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a")
        user_a = make_user(tenant_id=TENANT_A, email=shared_email)

    with tenant_scope(TENANT_B):
        make_tenant(id=TENANT_B, slug="tenant-b")
        user_b = make_user(tenant_id=TENANT_B, email=shared_email)

    yield {"user_a": user_a.id, "user_b": user_b.id}


def _raw_user_ids() -> set[uuid.UUID]:
    """完全不帶 tenant 條件的查詢——回傳什麼**全部由 RLS 決定**。"""
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM identity_user")
        return {row[0] for row in cursor.fetchall()}


# ── 表級設定：policy 有沒有真的開在該開的表上 ──────────────────


def test_every_tenant_scoped_identity_table_has_rls_forced() -> None:
    """六張表全部 ``ENABLE`` 且 ``FORCE`` row level security。

    ``FORCE`` 這一半特別容易漏：沒有它，表的 owner（也就是跑 migration 的
    `lumina_owner`）讀寫時完全不受 policy 約束。而維運腳本走的正是那個角色，
    於是「用 owner 連進去看一下」會看到全部租戶的資料，且沒有任何提示。
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = ANY(%s)",
            [list(TENANT_SCOPED_IDENTITY_TABLES)],
        )
        rows = {name: (enabled, forced) for name, enabled, forced in cursor.fetchall()}

    missing = set(TENANT_SCOPED_IDENTITY_TABLES) - rows.keys()
    assert not missing, f"這些表不存在：{sorted(missing)}"

    not_enabled = [name for name, (enabled, _) in rows.items() if not enabled]
    not_forced = [name for name, (_, forced) in rows.items() if not forced]

    assert not not_enabled, f"未啟用 RLS：{sorted(not_enabled)}"
    assert not not_forced, (
        f"啟用了 RLS 但未 FORCE：{sorted(not_forced)}——owner 角色不受 policy 約束，"
        "而 migration 與維運腳本正是以它連線"
    )


def test_permissions_is_intentionally_global() -> None:
    """`permissions` 沒有 tenant_id、也不開 RLS——這是設計，不是漏做。

    它是 ``resource:action`` 代碼的字典表（`knowledge:write` 之類），內容對所有
    租戶完全相同，也不含任何客戶資料。硬要給它一個 tenant_id 的話，每個租戶都
    得有一份一模一樣的複本，而「兩個租戶的 knowledge:write 是不是同一個權限」
    會變成需要 join 才能回答的問題。

    這條測試存在的理由是**防止它被誤判為安全缺口**：日後有人稽核「哪些表沒開
    RLS」時，這裡會給出答案而不是讓人補一個沒有意義的 policy。
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'identity_permission'")
        row = cursor.fetchone()
        cursor.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'identity_permission' AND column_name = 'tenant_id'"
        )
        has_tenant_column = cursor.fetchone() is not None

    assert row is not None, "identity_permission 表不存在"
    assert row[0] is False, "permissions 開了 RLS——它是全域字典表，policy 會擋掉所有查詢"
    assert not has_tenant_column, "permissions 不該有 tenant_id（見本測試 docstring）"


# ── 讀取隔離 ────────────────────────────────────────────────────


def test_select_without_any_filter_returns_only_own_tenant(
    two_tenants_with_users: dict[str, uuid.UUID],
) -> None:
    """沒有 WHERE 條件的查詢，兩個租戶各自只看到自己那一列。

    這是整份卡最核心的一條：查詢裡沒有任何租戶條件，過濾**完全**來自 RLS。
    """
    with tenant_context(TENANT_A), unit_of_work():
        visible_to_a = _raw_user_ids()

    with tenant_context(TENANT_B), unit_of_work():
        visible_to_b = _raw_user_ids()

    assert visible_to_a == {two_tenants_with_users["user_a"]}
    assert visible_to_b == {two_tenants_with_users["user_b"]}


def test_tenants_table_exposes_only_the_current_tenant_row() -> None:
    """`tenants` 表自己也受 RLS 管：policy 比對的是 ``id``，不是 ``tenant_id``。

    容易被忽略的一張表——它存的是客戶名稱、方案、設定，跨租戶讀得到等於客戶
    名單外流。而它的 policy 形狀跟其他表不同（沒有 tenant_id 欄位），寫錯的話
    最可能的結果是「條件永遠成立」，也就是完全沒有保護。
    """
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a")
    with tenant_scope(TENANT_B):
        make_tenant(id=TENANT_B, slug="tenant-b")

    with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
        cursor.execute("SELECT id FROM identity_tenant")
        visible = {row[0] for row in cursor.fetchall()}

    assert visible == {TENANT_A}


def test_missing_tenant_setting_yields_no_rows_instead_of_an_error(
    two_tenants_with_users: dict[str, uuid.UUID],
) -> None:
    """沒有設定 ``app.tenant_id`` 時，查詢回空集合，而不是丟例外。

    這是 1A-2 的決定（選項 A）：policy 寫成
    ``tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid``。
    拿不到值時整個條件是 NULL，於是一列都不符合。

    為什麼不讓它報錯：真正該擋下「忘了設租戶」的地方是 `core/uow.py`，它已經
    直接 raise 了，應用路徑上不會有漏網的。DB 這層是最後一張網，網子的正確行為
    是「什麼都不給看」；讓它報錯的話，`make psql-app` 這種手動連線一進去就爆，
    而錯誤訊息（``invalid input syntax for type uuid``）跟租戶毫無關係。
    """
    assert two_tenants_with_users  # 資料確實存在，下面的空集合才有意義

    # 不進 unit_of_work，也不設 tenant context：連線上沒有 app.tenant_id。
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM identity_user")
        row = cursor.fetchone()

    assert row is not None
    assert row[0] == 0, (
        f"未設定租戶時看到 {row[0]} 列——policy 沒有 fail closed，"
        "或 current_setting 的 missing_ok 參數漏了"
    )


# ── 寫入隔離（USING 擋讀、WITH CHECK 擋寫，兩者要分開驗）────────


def test_insert_into_another_tenant_is_rejected() -> None:
    """在租戶 A 的 context 下，不能塞一列 tenant_id = B 的資料。

    這是 ``WITH CHECK`` 的職責。只寫 ``USING`` 的 policy 在這裡會**成功寫入**，
    然後那列資料從此對 A 隱形、對 B 可見——等於一個可以匿名污染他人資料的通道，
    而讀取測試永遠看不到它。
    """
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a")
    with tenant_scope(TENANT_B):
        make_tenant(id=TENANT_B, slug="tenant-b")

    with (
        pytest.raises(ProgrammingError, match="row-level security"),
        tenant_context(TENANT_A),
        unit_of_work(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "INSERT INTO identity_user "
            "(id, tenant_id, email, password_hash, display_name, status, created_at, updated_at) "
            "VALUES (%s, %s, 'intruder@example.com', 'x', 'x', 'active', now(), now())",
            [uuid.uuid4(), TENANT_B],
        )


def test_update_cannot_move_a_row_to_another_tenant(
    two_tenants_with_users: dict[str, uuid.UUID],
) -> None:
    """不能把自己的資料「搬」到別的租戶名下。

    形狀跟 INSERT 不同：這裡起點是一列合法的自有資料，``USING`` 會放行，
    真正該擋的是改完之後的樣子——那同樣是 ``WITH CHECK``。搬走的資料對原租戶
    消失、對目標租戶出現，兩邊都不會有錯誤訊息。
    """
    assert two_tenants_with_users

    with (
        pytest.raises(ProgrammingError, match="row-level security"),
        tenant_context(TENANT_A),
        unit_of_work(),
        connection.cursor() as cursor,
    ):
        cursor.execute("UPDATE identity_user SET tenant_id = %s", [TENANT_B])


def test_delete_cannot_touch_another_tenants_rows(
    two_tenants_with_users: dict[str, uuid.UUID],
) -> None:
    """刪除他人資料不會報錯，但**影響 0 列**。

    DELETE 的失敗形狀跟 INSERT / UPDATE 不同：policy 讓那些列在 A 的視角下不存在，
    所以刪除是「成功刪了 0 列」。這條測試釘住的是「B 的資料還在」——若哪天 policy
    被改成只管 SELECT，這裡會變成靜默的跨租戶刪除。
    """
    with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM identity_user WHERE id = %s", [two_tenants_with_users["user_b"]]
        )
        deleted = cursor.rowcount

    with tenant_context(TENANT_B), unit_of_work():
        still_there = _raw_user_ids()

    assert deleted == 0, "刪到了別的租戶的列"
    assert still_there == {two_tenants_with_users["user_b"]}


# ── 系統角色：tenant_id IS NULL 的例外 ──────────────────────────


def test_system_roles_are_visible_to_every_tenant() -> None:
    """`roles` 的 ``tenant_id`` 為 NULL 代表四個系統內建角色，所有租戶都看得到。

    這張表的 policy 因此必須寫成「``tenant_id IS NULL`` 或 ``tenant_id = 當前租戶``」。
    照一般形狀寫（只有後半）的話，NULL 與任何值比較的結果都是 NULL 而不是 true，
    **系統角色會對所有人隱形**——症狀是使用者明明被指派了 Owner，查出來卻沒有
    任何角色，而權限判定會安靜地退化成「什麼都不能做」。
    """
    # 名字**刻意不用** migration 種的那四個（owner/admin/editor/viewer）：
    # `uq_role_tenant_name` 是 (tenant_id, name) 的唯一約束，種子還在時這行會直接
    # IntegrityError。序列跑之所以看不到，是因為排在前面的 transactional 測試已經
    # 把種子 TRUNCATE 掉了——這條測試原本依賴「別人先破壞資料」才會過，而那個順序
    # 在平行下不成立。斷言要的只是「一個 tenant_id IS NULL 的角色兩邊都看得到」。
    system_role = make_system_role(name="system-role-rls-probe")

    with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
        cursor.execute("SELECT id FROM identity_role WHERE tenant_id IS NULL")
        visible_to_a = {row[0] for row in cursor.fetchall()}

    with tenant_context(TENANT_B), unit_of_work(), connection.cursor() as cursor:
        cursor.execute("SELECT id FROM identity_role WHERE tenant_id IS NULL")
        visible_to_b = {row[0] for row in cursor.fetchall()}

    assert system_role.id in visible_to_a
    assert system_role.id in visible_to_b


def test_custom_roles_stay_inside_their_tenant() -> None:
    """自訂角色（``tenant_id`` 非 NULL）仍然只有該租戶看得到。

    與上一條成對：放寬 NULL 的同時，不能把整張表放行。這兩條一起才說明
    policy 的條件寫對了範圍。
    """
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a")
        role_a = make_tenant_role(tenant_id=TENANT_A, name="tenant-a-role")

    with tenant_scope(TENANT_B):
        make_tenant(id=TENANT_B, slug="tenant-b")

    with tenant_context(TENANT_B), unit_of_work(), connection.cursor() as cursor:
        cursor.execute("SELECT id FROM identity_role WHERE tenant_id IS NOT NULL")
        visible_to_b = {row[0] for row in cursor.fetchall()}

    assert role_a.id not in visible_to_b


def test_permission_dictionary_is_readable_by_every_tenant() -> None:
    """字典表沒有 RLS，所以任何租戶（含未設租戶的連線）都讀得到。

    這是 `test_permissions_is_intentionally_global` 的行為版：前者查目錄，
    這條驗實際查詢確實通得過——若哪天有人替它加了 policy，症狀會是權限判定
    全面失效（查不到任何 permission code），而那個原因很難聯想到這張表。
    """
    # **不能用真實存在的碼**（原本是 `knowledge:write`）。字典表由 data migration 種
    # 資料，而 transactional 測試會 TRUNCATE 掉它——於是這條測試的成敗取決於「同一個
    # worker 在它之前有沒有跑過別的 transactional 測試」：跑過就空表、通過；單獨跑就
    # 撞唯一鍵。1C-5 加了第三支 seed migration 之後，這個潛在的順序相依真的翻出來了。
    #
    # 這條測試要驗的是「字典表沒有 RLS」，與碼的內容無關，因此用一個不可能被種下的碼。
    permission = make_permission(code="test:dictionary-visibility")

    with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
        cursor.execute("SELECT code FROM identity_permission WHERE id = %s", [permission.id])
        row = cursor.fetchone()

    assert row is not None and row[0] == "test:dictionary-visibility"


# ── 共用列（tenant_id IS NULL）的寫入方向（0011_rls_write_scope）──


def _shared_role_ids() -> set[uuid.UUID]:
    """繞過 RLS 查共用列——用 owner 連線，因為要看的正是「租戶看不到的那一側」。"""
    with connections["admin"].cursor() as cursor:
        cursor.execute("SELECT id FROM identity_role WHERE tenant_id IS NULL")
        return {row[0] for row in cursor.fetchall()}


def test_a_tenant_cannot_delete_the_shared_system_roles() -> None:
    """``DELETE FROM identity_role WHERE tenant_id IS NULL`` 一列都不能刪得動。

    0002 的 policy 是 ``FOR ALL``，而 DELETE **只檢查 ``USING``**——那個條件為了讓系統
    角色對所有租戶可見放行了 NULL，於是「讀得到」順帶變成「刪得掉」。一條 SQL 就讓
    全平台的權限判定退化成「什麼都不能做」，而執行它只需要應用角色的連線加任一租戶
    context（也就是任何一個未來的程式 bug 或 injection 的落點）。
    """
    ensure_identity_seed()
    before = _shared_role_ids()
    assert before, "種子沒進去，這條測試會假綠"

    with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM identity_role WHERE tenant_id IS NULL")
        deleted = cursor.rowcount

    assert deleted == 0
    assert _shared_role_ids() == before


def test_a_tenant_cannot_delete_the_shared_permission_grants() -> None:
    """授權列比角色本身更貴：它沒有任何 FK 保護，刪掉就是全平台同時失去權限。"""
    ensure_identity_seed()

    with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM identity_role_permission WHERE tenant_id IS NULL")
        deleted = cursor.rowcount

    with connections["admin"].cursor() as cursor:
        cursor.execute("SELECT count(*) FROM identity_role_permission WHERE tenant_id IS NULL")
        remaining = cursor.fetchone()[0]

    assert deleted == 0
    assert remaining > 0


def test_a_tenant_cannot_hijack_a_shared_role() -> None:
    """把共用列的 ``tenant_id`` 改成自己 = 讓它從所有其他租戶手上消失。

    ``FOR ALL`` 的 UPDATE 檢查 ``USING``（舊列，NULL 放行）+ ``WITH CHECK``（新列，是
    自己的租戶），兩邊都過——**而資料庫裡沒有任何一列被刪**，事後也看不出發生過什麼。
    """
    ensure_identity_seed()
    before = _shared_role_ids()

    with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE identity_role SET tenant_id = %s WHERE tenant_id IS NULL", [str(TENANT_A)]
        )
        updated = cursor.rowcount

    assert updated == 0
    assert _shared_role_ids() == before


def test_a_tenant_can_still_change_and_delete_its_own_roles() -> None:
    """收窄寫入方向的另一半：自己的列必須照常改得動、刪得掉。

    只驗「擋住了」的話，把 policy 寫成 ``USING (false)`` 也會全綠——而那會讓租戶自訂
    角色永遠改不動，症狀是「按下儲存沒反應也沒錯誤」（RLS 讓那一列不存在，影響 0 列）。
    """
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a")
        role = make_tenant_role(tenant_id=TENANT_A, name="own-role")

    with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
        cursor.execute("UPDATE identity_role SET name = 'renamed' WHERE id = %s", [str(role.id)])
        assert cursor.rowcount == 1
        cursor.execute("DELETE FROM identity_role WHERE id = %s", [str(role.id)])
        assert cursor.rowcount == 1


# ── 平台級表：應用角色只讀（0012_platform_table_grants）─────────


@pytest.mark.parametrize("statement", ["INSERT", "UPDATE", "DELETE"])
def test_the_permission_dictionary_is_read_only_for_the_application_role(statement: str) -> None:
    """全域權限字典沒有 RLS（見 `test_permissions_is_intentionally_global`），所以第二道
    防線只剩 GRANT。應用連線寫得動它 = 任何一個 injection 落點都能自己發權限碼。

    正當的寫入者只有 migration（owner），因此擋在 DB 的權限層而不是程式裡。
    """
    statements = {
        "INSERT": (
            "INSERT INTO identity_permission (id, code, description, created_at, updated_at)"
            " VALUES (gen_random_uuid(), 'test:forged', '', now(), now())"
        ),
        "UPDATE": "UPDATE identity_permission SET description = 'x'",
        "DELETE": "DELETE FROM identity_permission",
    }

    with (
        tenant_context(TENANT_A),
        unit_of_work(),
        pytest.raises(ProgrammingError, match="permission denied"),
        connection.cursor() as cursor,
    ):
        cursor.execute(statements[statement])


@pytest.mark.parametrize("statement", ["INSERT", "UPDATE", "DELETE"])
def test_the_tenant_directory_is_read_only_for_the_application_role(statement: str) -> None:
    """登入路由表（slug → tenant_id）同樣沒有 RLS——它必須在「還不知道是哪個租戶」時
    就查得到。寫得動它等於能把別人的 slug 指到自己的租戶，或讓某個租戶登不進來。

    它的正當寫入者是 `identity_tenant` 上的 ``SECURITY DEFINER`` trigger，那條路徑以
    函式擁有者（owner）的身分寫入，不受這裡的 REVOKE 影響——由
    `test_tenant_bootstrap.py` 驗它仍然會同步。
    """
    statements = {
        "INSERT": (
            "INSERT INTO identity_tenant_directory (tenant_id, slug, status)"
            " VALUES (gen_random_uuid(), 'forged', 'active')"
        ),
        "UPDATE": "UPDATE identity_tenant_directory SET status = 'suspended'",
        "DELETE": "DELETE FROM identity_tenant_directory",
    }

    with (
        tenant_context(TENANT_A),
        unit_of_work(),
        pytest.raises(ProgrammingError, match="permission denied"),
        connection.cursor() as cursor,
    ):
        cursor.execute(statements[statement])

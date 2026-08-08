"""驗收：DB 角色拆分（13 §3.1 的 1A-P1~P3、05 §5.1）。

**這一整檔驗的是一件事：RLS 上線後真的擋得住。** 而 RLS 有三種「開了卻無效」
的失敗模式，三種都**完全無症狀**——policy 建好、查詢正常回傳、測試全綠：

1. 連線角色是 superuser 或帶 ``BYPASSRLS``：policy 對它完全不適用。
2. 連線角色是表的 owner 而該表未 ``FORCE ROW LEVEL SECURITY``：owner 預設豁免。
3. 連線角色雖非 owner，但**是 owner 角色的成員**：它可以 ``SET ROLE`` 成 owner，
   於是第 2 種豁免對它同樣有效——這條最容易在「權限不夠就 GRANT 一下」時被打開。

因此本檔的斷言對象是**角色屬性與權限拓樸**，不是 policy 本身（policy 屬 1A-2）。
順序上必須先有這一檔：policy 寫在角色沒拆之前，得到的是一份看起來正確、
實際不生效的隔離。

**兩條連線**（13 §3.1 的 1A-P2）：``default`` = 應用角色、``admin`` = owner 角色。
pytest 需要 ``CREATE DATABASE``，而 RLS 要驗的是應用角色的行為，兩者不能是同一
條連線——若整程以特權角色跑，1A-2 的跨租戶矩陣會在 RLS 完全失效時全綠，那比
沒有測試更糟：它會主動背書一個不存在的保護。

用 ``transaction=True``：跨 alias 的可見性需要真的 commit，
``django_db`` 的交易包裹會讓 admin 建的東西在 default 那條連線上看不到。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import NamedTuple

import pytest
from django.db import ProgrammingError, connection, connections


class RoleAttributes(NamedTuple):
    """某條連線的 ``current_user`` 在 ``pg_roles`` 上的屬性。"""

    name: str
    is_superuser: bool
    bypasses_rls: bool
    can_create_db: bool
    can_create_role: bool


# 應用角色**不得**擁有的屬性。每一項都對應一種靜默失效或一次權限擴張：
#   is_superuser    → RLS 完全不適用
#   bypasses_rls    → 同上
#   can_create_db   → 應用能自建一個不受任何 policy 保護的資料庫
#   can_create_role → 應用能自建一個帶 BYPASSRLS 的角色（等於自我提權）
FORBIDDEN_APP_ROLE_ATTRIBUTES = (
    "is_superuser",
    "bypasses_rls",
    "can_create_db",
    "can_create_role",
)


def _role_attributes(alias: str) -> RoleAttributes:
    with connections[alias].cursor() as cursor:
        cursor.execute(
            "SELECT rolname, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname = current_user"
        )
        row = cursor.fetchone()

    assert row is not None, f"alias `{alias}` 的 current_user 不在 pg_roles"
    return RoleAttributes(*row)


# ── 應用角色（default alias）────────────────────────────────────


@pytest.mark.django_db
def test_application_role_has_no_privilege_that_defeats_rls() -> None:
    """應用連線不得是 superuser、不得 BYPASSRLS、不得能建庫或建角色。

    這條與 ``test_infra_postgres.py`` 的絆線重疊了前兩項，是刻意的：那條在沒有
    任何表啟用 RLS 時會 skip（1A-2 之前一直如此），本條無條件跑。角色拆分完成
    當下就要有紅綠燈，不能等到 policy 寫出來才知道拆對了沒有。
    """
    attributes = _role_attributes("default")
    offending = [name for name in FORBIDDEN_APP_ROLE_ATTRIBUTES if getattr(attributes, name)]

    assert not offending, (
        f"應用角色 `{attributes.name}` 帶有 {offending}——"
        "前兩項使 RLS policy 完全不適用，後兩項讓應用可自行造出不受保護的環境（05 §5.1）"
    )


@pytest.mark.django_db
def test_application_role_owns_no_table() -> None:
    """public schema 內不得有任何表由應用角色擁有。

    表的 owner 預設豁免 policy。Django migration 建表時的 owner 就是跑 migration
    的角色——所以「migration 走 admin alias」不是風格問題，它決定了 owner 是誰。
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tableowner = %s",
            [_role_attributes("default").name],
        )
        owned = [row[0] for row in cursor.fetchall()]

    assert not owned, (
        f"以下表的 owner 是應用角色：{owned}——owner 預設豁免 RLS policy。"
        "檢查 migration 是否誤走 default alias（應為 `manage.py migrate --database=admin`）"
    )


@pytest.mark.django_db(databases=["default", "admin"])
def test_application_role_is_not_a_member_of_the_owner_role() -> None:
    """應用角色不得是 owner 角色的成員。

    成員身分等於可 ``SET ROLE`` 成 owner，於是 owner 的豁免對它照樣成立——
    角色名字拆開了，權限沒拆。這條擋的是「應用少了某個權限 → 直接 GRANT owner
    給它」這個很自然但會廢掉整個拆分的修法。
    """
    owner = _role_attributes("admin").name

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_has_role(current_user, %s, 'MEMBER')", [owner])
        row = cursor.fetchone()

    assert row is not None
    assert row[0] is False, f"應用角色是 `{owner}` 的成員 → 可 SET ROLE 成 owner 並取得 RLS 豁免"


@pytest.mark.django_db
def test_application_role_cannot_create_objects_in_public_schema() -> None:
    """應用角色不得有 public schema 的 CREATE 權限（最小權限，CLAUDE.md SECURITY）。

    有 CREATE 就能自建表——而自建的表 owner 是它自己，天然豁免 policy。
    這是「應用角色不擁有任何表」那條在時間軸上的補強：那條驗現況，這條驗未來。
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
        row = cursor.fetchone()

    assert row is not None
    assert row[0] is False, "應用角色可在 public schema 建物件 → 可造出自己擁有、豁免 policy 的表"


@pytest.mark.django_db(transaction=True, databases=["default", "admin"])
def test_application_role_can_still_do_dml_on_owner_created_tables() -> None:
    """拆分後應用仍能對 owner 建的表做 CRUD——否則整個應用起不來。

    這條是上面幾條的配套。少了它，「把權限收乾淨」的正確方向會一路收到應用
    無法運作，而症狀要等到跑起應用才出現。
    """
    table = "_acc_role_dml"

    with connections["admin"].cursor() as admin_cursor:
        admin_cursor.execute(f"CREATE TABLE {table} (id int primary key, note text)")
        admin_cursor.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "
            + _role_attributes("default").name
        )

    # 下面幾行的 noqa: S608 —— 表名是本檔的常數，不來自任何外部輸入
    # （psycopg 無法參數化識別字，識別字只能用字串組）。
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"INSERT INTO {table} (id, note) VALUES (1, 'x')")  # noqa: S608
            cursor.execute(f"UPDATE {table} SET note = 'y' WHERE id = 1")  # noqa: S608
            cursor.execute(f"SELECT note FROM {table} WHERE id = 1")  # noqa: S608
            row = cursor.fetchone()
            cursor.execute(f"DELETE FROM {table} WHERE id = 1")  # noqa: S608

        assert row is not None and row[0] == "y"
    finally:
        with connections["admin"].cursor() as admin_cursor:
            admin_cursor.execute(f"DROP TABLE IF EXISTS {table}")


@pytest.mark.django_db(transaction=True, databases=["default", "admin"])
def test_application_role_cannot_create_table_even_if_it_tries() -> None:
    """直接下 DDL 必須被 PostgreSQL 拒絕，不是靠約定。

    與上一條的權限查詢分開寫：``has_schema_privilege`` 回報的是**該角色自身**的
    權限，PUBLIC 上的授權（PG15 之前 public schema 對 PUBLIC 預設帶 CREATE）
    不會出現在那裡，於是查詢說沒有、實際建得起來。這條走真實 DDL，沒有這個死角。
    """
    with pytest.raises(ProgrammingError), connection.cursor() as cursor:
        cursor.execute("CREATE TABLE _acc_should_not_exist (id int)")


# ── owner / migration 角色（admin alias）─────────────────────────


@pytest.mark.django_db(databases=["admin"])
def test_owner_role_is_not_superuser_but_can_create_databases() -> None:
    """owner 角色要能建庫（pytest 需要），但**不是** superuser。

    為什麼不乾脆用 initdb 的 superuser 跑測試與 migration：superuser 同時豁免
    RLS 與所有權限檢查，一旦它成為日常使用的角色，任何「權限不足」的錯誤都不會
    出現在開發流程裡——而 production 的 migration 若照著同一份流程跑，缺的授權
    要到那時才發現。非 superuser 的 owner 讓授權缺口在本機就現形。
    """
    attributes = _role_attributes("admin")

    assert attributes.is_superuser is False, (
        f"migration/測試角色 `{attributes.name}` 是 superuser——"
        "它會豁免所有權限檢查，本機測不出 production 會缺的授權"
    )
    assert attributes.can_create_db is True, (
        "migration/測試角色缺 CREATEDB → pytest 無法建立 test database（1A-P2）"
    )


@pytest.mark.django_db(databases=["admin"])
def test_owner_role_has_no_statement_timeout() -> None:
    """5s 的 ``statement_timeout`` 只能套在應用角色上（13 §3.1 的 1A-P3）。

    套到 migration 角色的後果：大表的 ``AddIndexConcurrently`` 與 HNSW 建索引
    會在中途被砍，錯誤是 ``canceling statement due to statement timeout``，
    而那個時間點 schema 已經是半套的。

    這裡用「連上去 SHOW」而不是查 ``pg_db_role_setting``：後者對非特權角色不可讀，
    測試會因為權限而不是因為設定錯誤失敗，訊息指向完全錯誤的方向。
    """
    with connections["admin"].cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        row = cursor.fetchone()

    assert row is not None
    assert row[0] in ("0", ""), (
        f"migration 角色的 statement_timeout 是 {row[0]}，必須為 0（無限制）——"
        "否則 AddIndexConcurrently / HNSW 建索引會被砍在半途（11 §4.1 的 5s 只對應用連線）"
    )


@pytest.mark.django_db(databases=["admin"])
def test_owner_role_owns_every_table_in_public_schema() -> None:
    """所有表統一由 owner 角色擁有——單一 owner 才能讓 FORCE RLS 的推理成立。

    混合 owner（部分表由 superuser 建、部分由 owner 建）不會有任何症狀，但
    1A-2 之後每張表的豁免狀況都要個別推理，而「這張表的 owner 是誰」不會寫在
    migration 裡。
    """
    owner = _role_attributes("admin").name

    with connections["admin"].cursor() as cursor:
        cursor.execute(
            "SELECT tablename, tableowner FROM pg_tables "
            "WHERE schemaname = 'public' AND tableowner <> %s",
            [owner],
        )
        foreign = cursor.fetchall()

    assert not foreign, f"以下表的 owner 不是 `{owner}`：{foreign}"


# ── 端到端：拆分是否真的讓 RLS 生效 ─────────────────────────────


@pytest.fixture
def rls_probe_table() -> Iterator[str]:
    """由 owner 建一張開了 RLS + FORCE、policy 為 deny-all 的探針表。

    存在的理由：上面所有斷言都是「屬性正確」，而屬性正確不等於隔離成立——
    三種豁免路徑（superuser / owner / 成員身分）各自獨立，任何一條漏掉都讓
    policy 失效。這張表把三條路徑一次驗完，是本檔唯一的因果證明。
    """
    table = "_acc_rls_probe"

    with connections["admin"].cursor() as cursor:
        cursor.execute(f"CREATE TABLE {table} (id int primary key)")
        cursor.execute(f"INSERT INTO {table} (id) VALUES (1), (2)")  # noqa: S608
        cursor.execute(f"GRANT SELECT ON {table} TO " + _role_attributes("default").name)
        cursor.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE：讓 owner 自己也受 policy 管。少了它，owner 讀得到全部，
        # 而 owner 正是 migration 與維運腳本的身分。
        cursor.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        cursor.execute(f"CREATE POLICY {table}_deny_all ON {table} USING (false)")

    try:
        yield table
    finally:
        with connections["admin"].cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")


@pytest.mark.django_db(transaction=True, databases=["default", "admin"])
def test_deny_all_policy_actually_blocks_the_application_role(rls_probe_table: str) -> None:
    """deny-all policy 下應用連線必須讀到 0 列。

    這條紅燈時就是「RLS 開了但無效」——1A-2 的所有隔離測試在那個狀態下會全綠。
    """
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {rls_probe_table}")  # noqa: S608
        row = cursor.fetchone()

    assert row is not None
    assert row[0] == 0, (
        f"deny-all policy 下應用角色仍讀到 {row[0]} 列——"
        "RLS 對它不適用（superuser / BYPASSRLS / owner / owner 成員身分，四者之一）"
    )


@pytest.mark.django_db(transaction=True, databases=["default", "admin"])
def test_deny_all_policy_also_blocks_the_owner_role(rls_probe_table: str) -> None:
    """FORCE 之下 owner 自己也讀不到——驗的是 FORCE 真的有下。

    維運腳本走的就是 owner。少了 FORCE，「用 owner 連上去看一下」會看到全部
    租戶的資料，而那條路徑不經過任何應用層的租戶檢查。真正需要跨租戶的作業
    （backfill、DLQ 重放）另走顯式的 bypass 角色並留 Audit（05 §5.1）。
    """
    with connections["admin"].cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {rls_probe_table}")  # noqa: S608
        row = cursor.fetchone()

    assert row is not None
    assert row[0] == 0, (
        f"owner 角色在 FORCE RLS 下仍讀到 {row[0]} 列——確認 ALTER TABLE ... FORCE 有下"
    )

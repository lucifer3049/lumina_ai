"""驗收：PostgreSQL 是否照 05 資料庫設計長出來（extension / 型別 / collation）。

**為什麼這些要測而不是「看 Dockerfile 有寫就好」**：

1. extension 裝在 image 裡（`pg_available_extensions`）不等於裝在資料庫裡
   （`pg_extension`）。真正會爆的情境是後者——CI 的 test database 是從
   template1 新建的，若 extension 靠 `docker-entrypoint-initdb.d` 建立，
   `lumina` 有、`test_lumina` 沒有，測試在本機綠、在 CI 紅。
   所以本檔一律用 `django_db`（跑在 test database 上），逼 extension 必須
   走 Django migration 建立。
2. halfvec 與 HNSW ops 名稱（`halfvec_cosine_ops`）是 05 v1.1 的決策，
   pgvector 版本不足時 `CREATE INDEX` 才會失敗——只查 extension 存在測不出來。
3. pgroonga 選它的理由就是「不需自訂詞典即有中日韓斷詞」（05 §5.3）。
   所以斷言必須用中文查詢真的命中，而不是建完索引就算過。
"""

from __future__ import annotations

import pytest
from django.db import connection, connections

# 05 §5.3 / §3.2：向量檢索與中文 FTS 的兩個必要 extension。
REQUIRED_EXTENSIONS = {"vector", "pgroonga"}

# 05 §3.2：embeddings.vector 的維度與型別（day-1 即 halfvec）。
EMBEDDING_DIM = 1536


@pytest.mark.django_db
def test_required_extensions_installed_in_this_database() -> None:
    """vector / pgroonga 必須存在於**當前連線的資料庫**（不是只裝在 image 裡）。"""
    with connection.cursor() as cursor:
        cursor.execute("SELECT extname FROM pg_extension")
        installed = {row[0] for row in cursor.fetchall()}

    missing = REQUIRED_EXTENSIONS - installed
    assert not missing, (
        f"缺少 extension {sorted(missing)}——"
        "確認 CreateExtension migration 有跑（extension 不可用 initdb.d 建，"
        "那不會套用到 test database）"
    )


@pytest.mark.django_db
def test_rls_enabled_tables_are_actually_enforced() -> None:
    """絆線：只要有任何一張表開了 RLS，兩個前置條件就必須同時成立。

    RLS policy 屬工作包 1A（13 §3），Phase 0 一張都沒有，所以這條現在會 skip。
    它存在的理由是 RLS 有兩種「開了卻無效」的失敗模式，而兩者都**完全無症狀**
    ——policy 建好了、查詢正常回傳、測試全綠，隔離卻不存在：

    1. 連線角色是 superuser 或帶 ``BYPASSRLS``：policy 對它完全不適用。
    2. 連線角色是表的 owner 而該表沒有 ``FORCE ROW LEVEL SECURITY``：owner 預設
       豁免 policy。

    目前的 compose 恰好命中第 1 種——``POSTGRES_USER`` 同時是 initdb superuser、
    schema owner 與應用連線帳號（見 docker/compose.yml），而 05 §5.1 明確要求應用
    連線走非 superuser 角色。角色拆分要重建資料卷（``make clean``），因此列為 1A
    的前置工作而不在 Phase 0 動它。這條測試負責讓那件事不可能被忘記：1A 一旦下
    ``ENABLE ROW LEVEL SECURITY``，這裡就會紅燈，而不是安靜地失效。
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, relforcerowsecurity FROM pg_class "
            "WHERE relrowsecurity AND relkind = 'r'"
        )
        rls_tables = cursor.fetchall()

        if not rls_tables:
            pytest.skip("尚無啟用 RLS 的資料表——RLS 屬工作包 1A，屆時本條自動生效")

        cursor.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        role = cursor.fetchone()

    assert role is not None
    is_superuser, bypasses_rls = role

    assert not is_superuser, (
        "應用連線是 superuser → RLS policy 完全不適用（05 §5.1 要求非 superuser 角色）"
    )
    assert not bypasses_rls, "應用連線帶 BYPASSRLS → RLS policy 完全不適用"

    unforced = [name for name, forced in rls_tables if not forced]
    assert not unforced, (
        f"以下表啟用了 RLS 但未 FORCE：{unforced}——表的 owner 預設豁免 policy，"
        "而 migration 建表時的 owner 很可能正是應用角色，那 policy 等於沒開"
    )


@pytest.mark.django_db(databases=["admin"])
def test_halfvec_column_and_hnsw_index_are_usable() -> None:
    """halfvec(1536) 欄位 + HNSW `halfvec_cosine_ops` 索引可建、可查（05 §3.2、§4）。

    參數 m=16 / ef_construction=64 取自 05 §4 索引策略表——這裡連參數一起建，
    是為了讓「pgvector 版本太舊不支援 halfvec ops」在 Phase 0 就爆，
    而不是等 Phase 1C 建 embeddings 表時才發現要換 image。

    走 ``admin`` alias：角色拆分（13 §3.1）之後 default 是應用角色，它**沒有**
    public schema 的 CREATE 權限（見 tests/integration/test_db_roles.py）。
    這裡驗的是資料庫本身的能力，不是應用角色的權限，所以用 owner 連線是對的——
    要讓應用角色能跑這段 DDL，得把權限放寬到廢掉整個拆分。
    """
    with connections["admin"].cursor() as cursor:
        cursor.execute(f"CREATE TABLE _acc_vec (id int primary key, v halfvec({EMBEDDING_DIM}))")
        cursor.execute(
            "CREATE INDEX _acc_vec_hnsw ON _acc_vec "
            "USING hnsw (v halfvec_cosine_ops) WITH (m = 16, ef_construction = 64)"
        )

        near = "[" + ",".join(["1"] + ["0"] * (EMBEDDING_DIM - 1)) + "]"
        far = "[" + ",".join(["0"] * (EMBEDDING_DIM - 1) + ["1"]) + "]"
        cursor.execute(
            "INSERT INTO _acc_vec (id, v) VALUES (1, %s::halfvec), (2, %s::halfvec)",
            [near, far],
        )

        # 餘弦距離排序：與 near 相同的向量必須排第一。
        cursor.execute("SELECT id FROM _acc_vec ORDER BY v <=> %s::halfvec LIMIT 1", [near])
        row = cursor.fetchone()

    assert row is not None and row[0] == 1, "halfvec 餘弦距離排序結果不符預期"


@pytest.mark.django_db(databases=["admin"])
def test_pgroonga_matches_chinese_without_custom_dictionary() -> None:
    """pgroonga 索引對中文查詢命中，且不相關詞不誤中（05 §5.3 的選型理由）。

    走 ``admin`` alias 的理由同上一條（DDL 需要 owner 角色）。
    """
    with connections["admin"].cursor() as cursor:
        cursor.execute("CREATE TABLE _acc_fts (id int primary key, content text)")
        cursor.execute("CREATE INDEX _acc_fts_pgroonga ON _acc_fts USING pgroonga (content)")
        cursor.execute(
            "INSERT INTO _acc_fts (id, content) VALUES "
            "(1, '機器學習模型的評估方式與召回率指標'), "
            "(2, '員工出差費用報銷流程說明')"
        )
        # 強制走索引：pgroonga 未生效時 seq scan 也可能靠 LIKE 語意矇對，
        # 關掉 seqscan 讓「索引沒建成」直接以錯誤或錯誤結果現形。
        cursor.execute("SET LOCAL enable_seqscan = off")

        cursor.execute("SELECT id FROM _acc_fts WHERE content &@~ %s ORDER BY id", ["機器學習"])
        hits = [row[0] for row in cursor.fetchall()]

        cursor.execute("SELECT id FROM _acc_fts WHERE content &@~ %s", ["量子計算"])
        misses = cursor.fetchall()

    assert hits == [1], f"中文查詢命中結果不符預期：{hits}"
    assert misses == [], "不相關查詢不應命中"


@pytest.mark.django_db
def test_database_encoding_and_collation_match_spec() -> None:
    """UTF8 + `C` collation + `C.UTF-8` ctype（05 §5.5）。

    collation 只在 initdb 時決定，資料卷建立後改不了——漂掉的代價是重建資料庫，
    所以這條要在 Phase 0 釘住，不是上線前才查。
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_encoding_to_char(encoding), datcollate, datctype "
            "FROM pg_database WHERE datname = current_database()"
        )
        row = cursor.fetchone()

    assert row is not None
    encoding, collate, ctype = row
    assert encoding == "UTF8", f"encoding 是 {encoding}，規格是 UTF8"
    assert collate == "C", f"datcollate 是 {collate}，規格是 C（05 §5.5）"
    assert ctype.replace("_", "-").upper() == "C.UTF-8".upper(), (
        f"datctype 是 {ctype}，規格是 C.UTF-8（05 §5.5）"
    )


@pytest.mark.django_db
def test_server_timezone_is_utc() -> None:
    """全庫 UTC（05 §5.5）：顯示時區是前端的事，DB 不做在地化。"""
    with connection.cursor() as cursor:
        cursor.execute("SHOW timezone")
        row = cursor.fetchone()

    assert row is not None
    assert row[0].upper() == "UTC", f"timezone 是 {row[0]}，規格是 UTC"

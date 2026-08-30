"""驗收：向量欄位從 halfvec(1536) 改成 halfvec(1024)（bge-m3）。

**這是一次不可逆的全庫重建**（2026-08-30 人類裁決）。06 §2.2 的四步重嵌入靠
`UNIQUE(chunk, model, embedding_version)` 讓新舊兩版並存、舊版持續服務、隨時可回退
——但那個流程默默假設了**維度不變**：halfvec 是固定寬度的欄位型別，1024 與 1536
塞不進同一欄，所以「兩版並存」在換維度時不成立。裁決是不加第二欄、不開新表，直接
改欄位並清空既有向量。

因此這一組測試守的是三件會安靜出錯的事：

1. **欄位真的是 1024**。改了設定沒改 migration（或反過來）時，服務照樣起得來，
   而每一次寫入被 DB 以「expected N dimensions」擋下——錯誤指向 INSERT，真正的
   原因在幾層之外。
2. **HNSW 索引跟著重建、且 opclass 仍對得上欄位型別**。改欄位型別會連帶影響索引；
   `vector_cosine_ops` 套在 halfvec 上不會報錯，它只是**不會被查詢用到**——症狀是
   檢索從 30ms 變成整表掃描（同 1C-2 的理由，見 test_embeddings.py）。
3. **舊維度真的進不來**。清空之後若還有任何路徑寫得進 1536 維，那筆資料會一直
   活著，而它與新向量的距離沒有意義。
"""

from __future__ import annotations

import uuid

import pytest
from django.db import Error as DatabaseError
from django.db import connection

from apps.knowledge.models import Embedding
from config.settings.app_settings import get_app_settings
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_chunk, make_document, make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)

MODEL = "BAAI/bge-m3"
OLD_DIMENSIONS = 1536
NEW_DIMENSIONS = 1024


@pytest.fixture
def chunk_id() -> uuid.UUID:
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a")
        kb = make_knowledge_base(tenant_id=TENANT_A)
        document = make_document(kb=kb)
        return make_chunk(document=document, seq=0, content="請假規定").id


def _column_type() -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            WHERE c.relname = %s AND a.attname = 'vector' AND a.attnum > 0
            """,
            [Embedding._meta.db_table],
        )
        row = cursor.fetchone()
    assert row is not None, f"{Embedding._meta.db_table} 沒有 vector 欄位"
    return str(row[0])


def _index_defs() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = %s",
            [Embedding._meta.db_table],
        )
        return [str(row[0]) for row in cursor.fetchall()]


class TestColumnWidth:
    def test_the_vector_column_is_halfvec_1024(self) -> None:
        assert _column_type() == f"halfvec({NEW_DIMENSIONS})", (
            f"向量欄位不是 halfvec({NEW_DIMENSIONS})——與 bge-m3 的輸出對不上"
        )

    def test_the_settings_dimension_matches_the_column(self) -> None:
        """設定與 schema 是同一個數字的兩份宣告。漂掉時服務照樣起得來，而 Gateway 的
        `_check_dimensions` 會用設定值判斷、DB 會用欄位寬度判斷——先擋下的那一個決定
        錯誤訊息長什麼樣，而兩者都不會說出「這兩邊不一致」。"""
        assert get_app_settings().ai_embedding_dimensions == NEW_DIMENSIONS
        assert _column_type() == f"halfvec({get_app_settings().ai_embedding_dimensions})"


class TestIndexSurvivesTheChange:
    def test_an_hnsw_index_still_exists(self) -> None:
        """改欄位型別會連帶動到索引。掉了索引不會有任何錯誤——檢索只是從 30ms 變成
        整表掃描，而在開發庫的資料量下那看起來完全正常。"""
        assert any("hnsw" in definition.lower() for definition in _index_defs()), (
            "embeddings 沒有 HNSW 索引——檢索會退成整表掃描而不報錯"
        )

    def test_the_opclass_still_matches_the_column_type(self) -> None:
        """`vector_cosine_ops` 套在 halfvec 欄位上**不會報錯**，它只是不會被用到。"""
        hnsw = [d for d in _index_defs() if "hnsw" in d.lower()]

        assert hnsw, "沒有 HNSW 索引可檢查 opclass"
        assert any("halfvec_cosine_ops" in definition for definition in hnsw), (
            "HNSW 的 opclass 不是 halfvec_cosine_ops——索引建得起來但查詢用不到"
        )


class TestOldVectorsCannotComeBack:
    def test_a_1024_dimension_vector_round_trips(self, chunk_id: uuid.UUID) -> None:
        with tenant_scope(TENANT_A):
            Embedding.objects.create(
                tenant_id=TENANT_A,
                chunk_id=chunk_id,
                model=MODEL,
                embedding_version=1,
                vector=[0.5] * NEW_DIMENSIONS,
            )
            stored = Embedding.objects.get(chunk_id=chunk_id, model=MODEL)

        assert len(stored.vector) == NEW_DIMENSIONS

    def test_a_1536_dimension_vector_is_rejected(self, chunk_id: uuid.UUID) -> None:
        """舊維度必須進不來。留一條寫得進去的路徑，那筆資料會與新向量混在同一個
        索引裡，而它們之間的距離沒有意義——檢索不會報錯，只會安靜地變差。"""
        with tenant_scope(TENANT_A), pytest.raises(DatabaseError):
            Embedding.objects.create(
                tenant_id=TENANT_A,
                chunk_id=chunk_id,
                model=MODEL,
                embedding_version=1,
                vector=[0.5] * OLD_DIMENSIONS,
            )


class TestTheRebuildLeftNothingBehind:
    def test_no_embedding_survives_the_migration(self) -> None:
        """全庫重建的定義：migration 跑完，`embeddings` 是空的。

        沒清乾淨的話，`ALTER COLUMN ... TYPE halfvec(1024)` 會直接失敗（既有列的寬度
        不符），而那是一個半套的 schema——比失敗更糟的是它在**部分**環境成功（空庫的
        CI 綠燈、有資料的開發機紅燈），於是問題看起來像機器的問題。
        """
        assert Embedding.objects.count() == 0, (
            "migration 之後仍有舊向量——它們的維度與新欄位對不上"
        )

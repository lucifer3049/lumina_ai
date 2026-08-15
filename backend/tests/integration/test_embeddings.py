"""驗收：embeddings 資料層與 pgvector 索引（05 §3.2/§4、13 §3 工作包 1C-2）。

這一層是檢索的地基。四件事錯了都不會有錯誤訊息，只會讓 1C-4 的檢索安靜地變差或變慢：

1. **halfvec 存得回來**。fp16 有精度損失（05 §3.2 明列的取捨），但那必須是「小數點後
   三位左右」而不是「完全不同的數字」——後者代表寫入路徑轉錯了型別。
2. **HNSW 索引真的建起來、且 ops 對得上欄位型別**。`vector_cosine_ops` 套在 halfvec
   欄位上不會報錯，它只是**不會被查詢用到**——症狀是檢索從 30ms 變成整表掃描。
3. **`UNIQUE(chunk_id, model, embedding_version)`**。重嵌入是 at-least-once 的背景
   工作（06 §2.2），沒有這條約束時同一個 chunk 會累積多份同版本向量，檢索結果因此
   出現重複，而每一筆看起來都合法。
4. **RLS**。embeddings 是 chunks 的延伸，洩漏的後果一樣：別家的內容被當成本租戶的
   答案來源。
"""

from __future__ import annotations

import uuid

import pytest
from django.db import connection
from django.db.utils import IntegrityError

from apps.knowledge.models import Embedding
from repositories.knowledge import EmbeddingRepository
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_chunk, make_document, make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)

MODEL = "mock-embedding"


def _vector(seed: float = 0.5) -> list[float]:
    from config.settings.app_settings import get_app_settings

    return [seed] * get_app_settings().ai_embedding_dimensions


@pytest.fixture
def chunks() -> dict[uuid.UUID, list[uuid.UUID]]:
    """兩個租戶各一份文件、各三個 chunk。"""
    created: dict[uuid.UUID, list[uuid.UUID]] = {}
    for tenant_id, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=slug)
            kb = make_knowledge_base(tenant_id=tenant_id)
            document = make_document(kb=kb)
            created[tenant_id] = [
                make_chunk(document=document, seq=seq, content=f"chunk-{seq}").id
                for seq in range(3)
            ]
    return created


class TestVectorRoundTrip:
    def test_a_vector_survives_the_round_trip(
        self, chunks: dict[uuid.UUID, list[uuid.UUID]]
    ) -> None:
        """halfvec 是 fp16——**精度損失是設計取捨，不是 bug**（05 §3.2）。

        允許小數點後三位左右的誤差；差得比這多，代表寫入時轉錯型別（例如被當成
        字串或整數），而那種錯誤不會有例外，只會讓檢索結果毫無道理。
        """
        chunk_id = chunks[TENANT_A][0]
        original = [round(0.1 * (index % 7), 3) for index in range(len(_vector()))]

        with tenant_scope(TENANT_A):
            EmbeddingRepository().upsert(
                [{"chunk_id": chunk_id, "vector": original}], model=MODEL, embedding_version=1
            )
            stored = Embedding.objects.get(chunk_id=chunk_id)

        assert len(stored.vector) == len(original)
        assert all(abs(float(a) - b) < 0.001 for a, b in zip(stored.vector, original, strict=True))

    def test_the_dimension_matches_the_configured_model(self) -> None:
        """欄位維度必須與 `ai_embedding_dimensions` 一致。

        兩邊漂掉時，寫入會被 DB 以「expected N dimensions」擋下——而錯誤訊息指向
        INSERT，看不出是設定與 migration 不同步。換模型時兩者要一起改。
        """
        from config.settings.app_settings import get_app_settings

        field = Embedding._meta.get_field("vector")

        assert field.dimensions == get_app_settings().ai_embedding_dimensions


class TestUniqueness:
    def test_the_same_chunk_model_and_version_cannot_be_duplicated(
        self, chunks: dict[uuid.UUID, list[uuid.UUID]]
    ) -> None:
        chunk_id = chunks[TENANT_A][0]

        with tenant_scope(TENANT_A), pytest.raises(IntegrityError):
            Embedding.objects.create(
                tenant_id=TENANT_A,
                chunk_id=chunk_id,
                model=MODEL,
                embedding_version=1,
                vector=_vector(),
            )
            Embedding.objects.create(
                tenant_id=TENANT_A,
                chunk_id=chunk_id,
                model=MODEL,
                embedding_version=1,
                vector=_vector(0.2),
            )

    def test_two_models_can_coexist_for_one_chunk(
        self, chunks: dict[uuid.UUID, list[uuid.UUID]]
    ) -> None:
        """同一個 chunk 可以同時有多個模型／版本的向量（06 §2.2 的原子切換前提）。

        重嵌入的做法是「新版本算完 → 原子切換 → 清理舊版」，那需要兩個版本並存的
        那段時間。約束若少了 model 或 version，這條路就走不通。
        """
        chunk_id = chunks[TENANT_A][0]

        with tenant_scope(TENANT_A):
            repository = EmbeddingRepository()
            repository.upsert(
                [{"chunk_id": chunk_id, "vector": _vector()}], model=MODEL, embedding_version=1
            )
            repository.upsert(
                [{"chunk_id": chunk_id, "vector": _vector(0.2)}], model="other", embedding_version=1
            )

            assert Embedding.objects.filter(chunk_id=chunk_id).count() == 2


class TestUpsert:
    def test_writing_twice_updates_instead_of_failing(
        self, chunks: dict[uuid.UUID, list[uuid.UUID]]
    ) -> None:
        """重嵌入是 at-least-once 的背景工作——重跑必須安全（08 §6 的同一個理由）。

        以唯一約束 + `ON CONFLICT` 達成，而不是「先查再寫」：併發的兩個 worker 都會
        查到「不存在」，然後其中一個撞約束失敗，那一批的整份工作就白做了。
        """
        chunk_id = chunks[TENANT_A][0]

        with tenant_scope(TENANT_A):
            repository = EmbeddingRepository()
            repository.upsert(
                [{"chunk_id": chunk_id, "vector": _vector(0.1)}], model=MODEL, embedding_version=1
            )
            repository.upsert(
                [{"chunk_id": chunk_id, "vector": _vector(0.9)}], model=MODEL, embedding_version=1
            )

            stored = Embedding.objects.get(chunk_id=chunk_id, model=MODEL, embedding_version=1)
            assert Embedding.objects.filter(chunk_id=chunk_id).count() == 1
            assert abs(float(stored.vector[0]) - 0.9) < 0.001

    def test_missing_chunks_are_reported(self, chunks: dict[uuid.UUID, list[uuid.UUID]]) -> None:
        """「這批 chunk 裡哪些還沒有向量」是 1C-3 的批次依據。

        少了它，重跑會把整份文件重算一次——那是真的錢（每個 chunk 一次 API 呼叫）。
        """
        chunk_ids = chunks[TENANT_A]

        with tenant_scope(TENANT_A):
            repository = EmbeddingRepository()
            repository.upsert(
                [{"chunk_id": chunk_ids[0], "vector": _vector()}], model=MODEL, embedding_version=1
            )

            pending = repository.chunks_without_embedding(
                chunk_ids, model=MODEL, embedding_version=1
            )

        assert set(pending) == set(chunk_ids[1:])


class TestTenantIsolation:
    def test_the_repository_never_returns_another_tenant_rows(
        self, chunks: dict[uuid.UUID, list[uuid.UUID]]
    ) -> None:
        with tenant_scope(TENANT_B):
            EmbeddingRepository().upsert(
                [{"chunk_id": chunks[TENANT_B][0], "vector": _vector()}],
                model=MODEL,
                embedding_version=1,
            )

        with tenant_scope(TENANT_A):
            visible = EmbeddingRepository().for_chunks(
                chunks[TENANT_B], model=MODEL, embedding_version=1
            )

        assert visible == []

    def test_rls_is_enabled_and_forced(self) -> None:
        """policy 之外還要 FORCE：owner 建的表對 owner 預設豁免 policy（13 §3.1）。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'knowledge_embedding'"
            )
            enabled, forced = cursor.fetchone()

        assert enabled and forced


class TestIndexes:
    def test_the_hnsw_index_exists_with_matching_ops(self) -> None:
        """HNSW + **halfvec** 的 ops（05 §4）。

        `vector_cosine_ops` 套在 halfvec 欄位上不會報錯，它只是永遠不會被查詢選用
        ——症狀是檢索從數十毫秒變成整表掃描，而結果完全正確。這種退化沒有任何錯誤
        訊息，只有在資料量長大之後才以「怎麼越來越慢」的形式出現。
        """
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'knowledge_embedding' AND indexdef ILIKE '%hnsw%'"
            )
            definitions = [row[0] for row in cursor.fetchall()]

        assert definitions, "沒有 HNSW 索引"
        definition = definitions[0]
        assert "halfvec_cosine_ops" in definition
        assert "m='16'" in definition and "ef_construction='64'" in definition

    def test_the_lookup_index_covers_tenant_and_chunk(self) -> None:
        """`(tenant_id, chunk_id)`（05 §4）：1C-3 的「哪些 chunk 已經有向量」走這條。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'knowledge_embedding'"
            )
            definitions = " ".join(row[0] for row in cursor.fetchall())

        assert "tenant_id, chunk_id" in definitions

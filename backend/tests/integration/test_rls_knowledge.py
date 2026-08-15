"""驗收：Knowledge 四張表的 RLS（05 §5.1、13 §3 工作包 1B）。

與 `test_rls_identity.py` 同一套方法論，不重述理由，只記差異：

- **查詢一律繞過 Repository、直接下沒有 WHERE tenant_id 的原生 SQL。** 用 ORM 查會
  同時經過 filter 與 policy 兩道防線，綠燈分不出是哪一道擋的。
- **四種寫入路徑分開驗**（SELECT / INSERT / UPDATE / DELETE）：``USING`` 管看得到
  哪些列，``WITH CHECK`` 管寫進去的列長什麼樣。只寫 ``USING`` 的 policy 擋得住讀、
  擋不住把資料寫進別的租戶名下，而讀取測試看不到這個漏洞。

**為什麼 chunks 特別重要**：它是 RAG 檢索實際讀的那張表。identity 的表洩漏是「看到
別家的使用者清單」，chunks 洩漏是「別家的文件內容被當成本租戶的答案來源餵進 LLM，
再連同引用一起回給使用者」——那同時是資料外洩與答案汙染，而且回應看起來完全正常。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from django.db import connection
from django.db.utils import ProgrammingError

from core.tenant import tenant_context
from core.uow import unit_of_work
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_chunk, make_document, make_etl_job, make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)

# Knowledge 的五張表，全部需要隔離（沒有 identity 那種全域字典表的例外）。
KNOWLEDGE_TABLES = (
    "knowledge_knowledgebase",
    "knowledge_document",
    "knowledge_chunk",
    "knowledge_etljob",
    # 1C-2 新增。**新表一律要加進這個清單**：漏掉的表不會有任何症狀——查詢照常
    # 回傳，只是範圍變成整個資料庫。embeddings 的洩漏後果與 chunks 相同，而且它
    # 是檢索**實際比對**的東西（chunk 的文字只是事後拿來顯示的）。
    "knowledge_embedding",
)


@pytest.fixture
def two_tenants_with_documents() -> Iterator[dict[str, uuid.UUID]]:
    """兩個租戶各一個 KB、一份文件、一個 chunk、一個 etl_job。

    **兩份文件的 content_hash 故意相同**：它同時驗兩件事——去重約束是租戶內（加 KB
    內）唯一而不是全域唯一，以及跨租戶查詢不會因為 hash 相同就撈錯文件。同一份公開
    PDF 被兩家客戶各自上傳是完全正常的情境。
    """
    shared_hash = "a" * 64

    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a")
        kb_a = make_knowledge_base(tenant_id=TENANT_A, name="KB A")
        doc_a = make_document(kb=kb_a, filename="shared.pdf", content_hash=shared_hash)
        chunk_a = make_chunk(document=doc_a, content="租戶 A 的機密內容")
        job_a = make_etl_job(document=doc_a)

    with tenant_scope(TENANT_B):
        make_tenant(id=TENANT_B, slug="tenant-b")
        kb_b = make_knowledge_base(tenant_id=TENANT_B, name="KB B")
        doc_b = make_document(kb=kb_b, filename="shared.pdf", content_hash=shared_hash)
        chunk_b = make_chunk(document=doc_b, content="租戶 B 的機密內容")
        job_b = make_etl_job(document=doc_b)

    yield {
        "kb_a": kb_a.id,
        "kb_b": kb_b.id,
        "doc_a": doc_a.id,
        "doc_b": doc_b.id,
        "chunk_a": chunk_a.id,
        "chunk_b": chunk_b.id,
        "job_a": job_a.id,
        "job_b": job_b.id,
    }


def _raw_ids(table: str) -> set[uuid.UUID]:
    """完全不帶 tenant 條件的查詢——回傳什麼**全部由 RLS 決定**。"""
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT id FROM {table}")  # noqa: S608 —— 表名來自本檔常數
        return {row[0] for row in cursor.fetchall()}


class TestRlsIsEnabledOnEveryTable:
    """policy 的存在性——四個條件缺一即等於沒開，而且全部無症狀。"""

    @pytest.mark.parametrize("table", KNOWLEDGE_TABLES)
    def test_table_has_rls_enabled_and_forced(self, table: str) -> None:
        """``ENABLE`` ＋ ``FORCE`` 都要有。

        少了 FORCE，跑 migration 與維運腳本的 owner 角色讀寫時完全不受 policy 約束
        ——而那正是清理 worker 與 backfill 會用的角色。
        """
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
                [table],
            )
            row = cursor.fetchone()

        assert row is not None, f"表 {table} 不存在"
        enabled, forced = row
        assert enabled, f"{table} 沒有 ENABLE ROW LEVEL SECURITY"
        assert forced, f"{table} 沒有 FORCE ROW LEVEL SECURITY（owner 會豁免 policy）"

    @pytest.mark.parametrize("table", KNOWLEDGE_TABLES)
    def test_policy_has_both_using_and_with_check(self, table: str) -> None:
        """``USING`` 與 ``WITH CHECK`` 兩個條件都要在。

        只有 USING 的 policy 擋得住讀、擋不住寫進別的租戶名下。這條測試讀
        ``pg_policies`` 而不是靠行為推論，因為「寫入被擋」有很多種原因（FK、約束），
        分辨不出是不是 policy 擋的。
        """
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT qual, with_check FROM pg_policies WHERE tablename = %s AND policyname = %s",
                [table, "tenant_isolation"],
            )
            row = cursor.fetchone()

        assert row is not None, f"{table} 沒有 tenant_isolation policy"
        qual, with_check = row
        assert qual, f"{table} 的 policy 缺 USING"
        assert with_check, f"{table} 的 policy 缺 WITH CHECK（擋不住寫進別的租戶）"


class TestSelectIsolation:
    @pytest.mark.parametrize(
        ("table", "own_key", "other_key"),
        [
            ("knowledge_knowledgebase", "kb_a", "kb_b"),
            ("knowledge_document", "doc_a", "doc_b"),
            ("knowledge_chunk", "chunk_a", "chunk_b"),
            ("knowledge_etljob", "job_a", "job_b"),
        ],
    )
    def test_each_tenant_only_sees_its_own_rows(
        self,
        two_tenants_with_documents: dict[str, uuid.UUID],
        table: str,
        own_key: str,
        other_key: str,
    ) -> None:
        with tenant_context(TENANT_A), unit_of_work():
            visible = _raw_ids(table)

        assert visible == {two_tenants_with_documents[own_key]}
        assert two_tenants_with_documents[other_key] not in visible

    def test_no_tenant_context_sees_nothing(
        self, two_tenants_with_documents: dict[str, uuid.UUID]
    ) -> None:
        """沒有交易區域參數時一列都看不到（fail closed）。

        policy 的租戶值取不到時整個條件是 NULL，於是沒有列符合。**不是報錯**——
        報錯的話 ``make psql-app`` 這種手動連線一進去就爆，而訊息與租戶無關
        （見 identity 的 0002_rls.py docstring）。真正該擋下「忘了設租戶」的是
        `core/uow.py`，它直接 raise。
        """
        assert two_tenants_with_documents  # 資料確實存在，下面的空集合才有意義

        for table in KNOWLEDGE_TABLES:
            assert _raw_ids(table) == set(), f"{table} 在沒有租戶 context 時仍回傳資料"


class TestInsertIsolation:
    def test_cannot_insert_a_row_for_another_tenant(
        self, two_tenants_with_documents: dict[str, uuid.UUID]
    ) -> None:
        """在租戶 A 的交易裡把 chunk 寫成租戶 B 的——``WITH CHECK`` 必須擋下。

        這是**最容易漏掉的一種洩漏**：不是讀到別人的資料，而是把資料塞進別人的
        名下。ETL 的寫入路徑（chunk 落庫）如果有一個地方拿錯 tenant_id，受害者是
        那個被塞資料的租戶——他的檢索結果裡會混進不屬於他的內容。
        """
        with (
            pytest.raises(ProgrammingError),
            tenant_context(TENANT_A),
            unit_of_work(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                INSERT INTO knowledge_chunk
                    (id, tenant_id, document_id, kb_id, seq, content, token_count,
                     chunk_version, doc_version, meta, superseded, created_at, updated_at)
                VALUES (%s, %s, %s, %s, 0, 'injected', 1, 1, 1, '{}', false, now(), now())
                """,
                [
                    uuid.uuid4(),
                    TENANT_B,
                    two_tenants_with_documents["doc_b"],
                    two_tenants_with_documents["kb_b"],
                ],
            )


class TestUpdateIsolation:
    def test_cannot_update_another_tenants_rows(
        self, two_tenants_with_documents: dict[str, uuid.UUID]
    ) -> None:
        """不帶 WHERE 的 UPDATE 只會動到自己的列。

        ``UPDATE knowledge_chunk SET superseded = true`` 是 re-ingest 的真實寫法
        （標記舊版本）。少了 policy，一次 re-ingest 會把**所有租戶**的 chunk 標成
        superseded——受害租戶的檢索會突然回傳空集合，而沒有任何錯誤。
        """
        with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
            cursor.execute("UPDATE knowledge_chunk SET superseded = true")
            assert cursor.rowcount == 1, "UPDATE 影響的列數不是 1——policy 沒有限制範圍"

        with tenant_context(TENANT_B), unit_of_work(), connection.cursor() as cursor:
            cursor.execute("SELECT superseded FROM knowledge_chunk")
            assert [row[0] for row in cursor.fetchall()] == [False], "租戶 B 的 chunk 被改到了"


class TestDeleteIsolation:
    def test_cannot_delete_another_tenants_rows(
        self, two_tenants_with_documents: dict[str, uuid.UUID]
    ) -> None:
        """不帶 WHERE 的 DELETE 同理——清理 worker 的真實形狀。"""
        with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
            cursor.execute("DELETE FROM knowledge_chunk")
            assert cursor.rowcount == 1, "DELETE 影響的列數不是 1——policy 沒有限制範圍"

        with tenant_context(TENANT_B), unit_of_work(), connection.cursor() as cursor:
            cursor.execute("SELECT id FROM knowledge_chunk")
            remaining = {row[0] for row in cursor.fetchall()}

        assert remaining == {two_tenants_with_documents["chunk_b"]}, "租戶 B 的 chunk 被刪掉了"


class TestDedupeConstraintIsTenantScoped:
    def test_same_content_hash_in_two_tenants_is_allowed(
        self, two_tenants_with_documents: dict[str, uuid.UUID]
    ) -> None:
        """兩個租戶各有一份 content_hash 相同的文件——fixture 已建立，這裡確認它成立。

        `UNIQUE(tenant_id, kb_id, content_hash)` 若被寫成全域唯一，fixture 本身就會
        在建立租戶 B 的文件時炸掉。這條測試明確把它列為斷言，而不是依賴 fixture
        「碰巧沒出錯」——那種依賴在有人改動約束時不會有紅燈，只會有一個看不懂的
        fixture 錯誤。
        """
        assert two_tenants_with_documents["doc_a"] != two_tenants_with_documents["doc_b"]

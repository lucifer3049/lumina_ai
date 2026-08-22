"""驗收：Knowledge Repository —— 租戶隔離的第一道防線（鐵則 4、13 §3 工作包 1B）。

第二道（RLS）由 `test_rls_knowledge.py` 驗。兩道分開測是刻意的，因為它們以不同方式
失效：這裡驗「程式沒有繞過 filter」，那裡驗「就算繞過了，DB 也擋得住」。

本檔另外釘住三個查詢語意，它們錯了都不會報錯、只會回傳看起來合理的錯誤結果：

1. **chunk 檢索預設排除 superseded**——舊版本的殘留混進檢索結果，答案會引用到早已
   被取代的內容，而引用連結本身是有效的。
2. **文件去重是 (tenant, kb, content_hash)**——同一份文件放進兩個 KB 是正當需求。
3. **etl_job 以 (document, doc_version, stage) 定位**——冪等鍵（08 §6）。查錯的話
   重跑會建出第二筆 job，而「這個階段跑過了嗎」從此答不準。
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest import mock

import pytest
from django.db import connections

from apps.knowledge.models import EtlJob
from core.db import run_orm
from core.exceptions import TenantContextMissingError
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.knowledge import (
    ChunkRepository,
    DocumentRepository,
    EtlJobRepository,
    KnowledgeBaseRepository,
)
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_chunk, make_document, make_etl_job, make_knowledge_base

# `admin` 也要列進來：`TestEtlJobAttemptCounter` 用它扮演「另一個 worker 的連線」。
pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

ALL_REPOSITORIES = (
    KnowledgeBaseRepository,
    DocumentRepository,
    ChunkRepository,
    EtlJobRepository,
)


@pytest.fixture
def two_tenants_with_content() -> dict[str, uuid.UUID]:
    """兩個租戶，各一個 KB 與一份文件；租戶 A 的文件另有三個 chunk。"""
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a")
        kb_a = make_knowledge_base(tenant_id=TENANT_A, name="KB A")
        doc_a = make_document(kb=kb_a, filename="a.pdf")
        for seq in range(3):
            make_chunk(document=doc_a, seq=seq, content=f"a-chunk-{seq}")

    with tenant_scope(TENANT_B):
        make_tenant(id=TENANT_B, slug="tenant-b")
        kb_b = make_knowledge_base(tenant_id=TENANT_B, name="KB B")
        doc_b = make_document(kb=kb_b, filename="b.pdf")
        make_chunk(document=doc_b, seq=0, content="b-chunk-0")

    return {"kb_a": kb_a.id, "kb_b": kb_b.id, "doc_a": doc_a.id, "doc_b": doc_b.id}


class TestTenantScoping:
    """每個 Repository 都必須繼承 `TenantScopedRepository` 的行為。"""

    @pytest.mark.parametrize("repository", ALL_REPOSITORIES)
    async def test_missing_tenant_context_raises(self, repository: type) -> None:
        """缺 TenantContext 一律 raise（Fail Fast），不得默默回傳全部資料。

        逐個 Repository 驗而不是抽一個代表：漏繼承基底、或自行覆寫 `get_queryset`
        時忘了取租戶，都只會影響其中一個類別。而 chunk 那個是最不能漏的。
        """
        with pytest.raises(TenantContextMissingError):
            await run_orm(repository().get_queryset)

    async def test_documents_are_scoped_to_the_tenant(
        self, two_tenants_with_content: dict[str, uuid.UUID]
    ) -> None:
        def _filenames() -> set[str]:
            with unit_of_work():
                return set(DocumentRepository().get_queryset().values_list("filename", flat=True))

        with tenant_context(TENANT_A):
            assert await run_orm(_filenames) == {"a.pdf"}
        with tenant_context(TENANT_B):
            assert await run_orm(_filenames) == {"b.pdf"}

    async def test_looking_up_another_tenants_document_by_id_returns_none(
        self, two_tenants_with_content: dict[str, uuid.UUID]
    ) -> None:
        """拿著別的租戶的 id 直接查——必須是「查無此物」，不是回傳它。

        回 None 而不是 raise 是刻意的：API 層要把它轉成 404（09 §2.3 的資源類權限
        規則——回 403 等於承認「這個 id 存在，只是你不能碰」，那讓人可以拿 id 掃出
        別的租戶有哪些文件）。
        """

        def _get() -> object | None:
            with unit_of_work():
                return DocumentRepository().get_by_id(two_tenants_with_content["doc_b"])

        with tenant_context(TENANT_A):
            assert await run_orm(_get) is None


class TestChunkQuerySemantics:
    async def test_chunks_of_a_document_come_back_in_order(
        self, two_tenants_with_content: dict[str, uuid.UUID]
    ) -> None:
        """chunk 一律按 ``seq`` 排序。

        沒有明確 ORDER BY 時 PostgreSQL 的回傳順序不保證——小表通常剛好是插入順序，
        於是測試綠、開發環境看起來正常，而重寫過的表（VACUUM、re-ingest）會突然變成
        亂序。chunk 亂序的後果是「文件預覽讀起來語意錯亂」與「相鄰 chunk 拼接時
        接錯段落」。
        """

        def _sequence() -> list[str]:
            with unit_of_work():
                rows = ChunkRepository().for_document(two_tenants_with_content["doc_a"])
                return [row.content for row in rows]

        with tenant_context(TENANT_A):
            assert await run_orm(_sequence) == ["a-chunk-0", "a-chunk-1", "a-chunk-2"]

    async def test_superseded_chunks_are_excluded_by_default(
        self, two_tenants_with_content: dict[str, uuid.UUID]
    ) -> None:
        """檢索候選集預設排除 superseded（05 §4 的 partial index 就是為它建的）。

        superseded 是 re-ingest 之後舊版本的標記。混進檢索結果的話，LLM 會拿到早已
        被取代的內容當依據，而且引用指向的 chunk 確實存在——回應看起來完全正常，
        只是內容是舊的。這種錯誤沒有任何自動化手段能事後發現。
        """

        def _supersede_first_then_count() -> int:
            with unit_of_work():
                repo = ChunkRepository()
                first = repo.for_document(two_tenants_with_content["doc_a"])[0]
                repo.mark_superseded(chunk_ids=[first.id])
                return len(repo.for_retrieval(kb_id=two_tenants_with_content["kb_a"]))

        with tenant_context(TENANT_A):
            assert await run_orm(_supersede_first_then_count) == 2


class TestDocumentDedupe:
    async def test_same_hash_in_another_kb_is_a_different_document(
        self, two_tenants_with_content: dict[str, uuid.UUID]
    ) -> None:
        """同一份文件可以同時放進兩個 KB（去重鍵含 kb_id）。

        「法規彙編」與「新人訓練」各放一份同樣的 PDF 是正當需求。少了 kb_id，第二次
        上傳會被擋下並顯示「文件已存在」——而使用者在這個 KB 裡根本找不到它。
        """

        def _create_in_second_kb() -> uuid.UUID:
            with unit_of_work():
                repo = DocumentRepository()
                existing = repo.get_by_id(two_tenants_with_content["doc_a"])
                assert existing is not None
                other_kb = make_knowledge_base(tenant_id=TENANT_A, name="另一個 KB")
                created = repo.create(
                    kb_id=other_kb.id,
                    filename=existing.filename,
                    mime_type=existing.mime_type,
                    storage_key=f"{existing.storage_key}-copy",
                    content_hash=existing.content_hash,
                    size_bytes=existing.size_bytes,
                )
                return created.id

        with tenant_context(TENANT_A):
            assert await run_orm(_create_in_second_kb) is not None

    async def test_find_by_hash_is_scoped_to_the_kb(
        self, two_tenants_with_content: dict[str, uuid.UUID]
    ) -> None:
        """去重查詢（上傳前的「這份文件已經有了嗎」）同樣以 KB 為範圍。

        1B-3 的上傳流程會呼叫它；查詢範圍比約束寬的話，上傳會回報「重複」但實際
        INSERT 得進去，兩邊的判定不一致。
        """

        def _lookup_in_own_kb_and_in_an_empty_one() -> tuple[object | None, object | None]:
            # **整段包在同一個 UoW 裡**：每一次 DB 存取都需要交易區域參數
            # ``app.tenant_id``，少了它 RLS 會讓查詢回空、INSERT 被 policy 擋下
            # ——而「查不到」正是本測試後半的期望值，漏包的話後半會假綠燈。
            with unit_of_work():
                repo = DocumentRepository()
                document = repo.get_by_id(two_tenants_with_content["doc_a"])
                assert document is not None
                empty_kb = make_knowledge_base(tenant_id=TENANT_A, name="空 KB")
                return (
                    repo.find_by_content_hash(
                        kb_id=two_tenants_with_content["kb_a"],
                        content_hash=document.content_hash,
                    ),
                    repo.find_by_content_hash(
                        kb_id=empty_kb.id, content_hash=document.content_hash
                    ),
                )

        with tenant_context(TENANT_A):
            in_own_kb, in_empty_kb = await run_orm(_lookup_in_own_kb_and_in_an_empty_one)

        assert in_own_kb is not None, "同 KB 內的同 hash 應查得到（上傳要判定為重複）"
        assert in_empty_kb is None, "別的 KB 不該查到——去重範圍含 kb_id"


class TestEtlJobIdempotencyKey:
    async def test_job_is_located_by_document_version_and_stage(
        self, two_tenants_with_content: dict[str, uuid.UUID]
    ) -> None:
        """冪等鍵是 ``(doc_id, doc_version, stage)``（08 §6）。

        少了 doc_version，re-ingest（doc_version+1）會查到上一版**已成功**的 job，
        於是新版本的該階段被判定為「跑過了」直接跳過——文件停在舊內容，狀態卻是
        ready。少了 stage 則是各階段互相覆蓋。
        """

        def _create_and_lookup() -> tuple[object | None, object | None]:
            with unit_of_work():
                repo = EtlJobRepository()
                doc_id = two_tenants_with_content["doc_a"]
                document = DocumentRepository().get_by_id(doc_id)
                assert document is not None
                make_etl_job(document=document, stage="extract", status="succeeded")
                same_version = repo.find(doc_id=doc_id, doc_version=1, stage="extract")
                next_version = repo.find(doc_id=doc_id, doc_version=2, stage="extract")
                return same_version, next_version

        with tenant_context(TENANT_A):
            found, not_found = await run_orm(_create_and_lookup)

        assert found is not None, "同版本同階段應查得到（重跑要能判定為已完成）"
        assert not_found is None, "下一版的同階段不該查到上一版的 job（08 §6 冪等鍵）"


class TestEtlJobAttemptCounter:
    """``attempt`` 是「重試 ≤3」（08 §6）的唯一依據，所以它少算一次就是多跑一次。"""

    def test_each_start_counts_one_attempt(
        self, two_tenants_with_content: dict[str, uuid.UUID]
    ) -> None:
        with tenant_context(TENANT_A), unit_of_work():
            repo = EtlJobRepository()
            doc_id = two_tenants_with_content["doc_a"]
            repo.start(doc_id=doc_id, doc_version=1, stage="extract")
            job = repo.start(doc_id=doc_id, doc_version=1, stage="extract")

        assert job.attempt == 2, "重跑同一個階段要累計，否則重試上限永遠達不到"

    def test_a_concurrent_start_is_not_lost(
        self, two_tenants_with_content: dict[str, uuid.UUID]
    ) -> None:
        """併發的兩次 `start()` 必須各算一次。

        ``job.attempt += 1`` 是 read-modify-write：兩個觸發（重試與排程撞在一起——
        `EtlJob` 的 Meta 自己就舉了這個情境）各自讀到同一個舊值、各自寫回 +1，於是兩次
        執行只加了一次。症狀是 ``≤3`` 的上限悄悄變成第 4 次，而 job 那一列看起來完全正常。

        這裡把那個交錯**確定性地**重現：在 repository 讀到列之後、寫回之前，讓另一條
        連線（另一個 worker）先遞增並 COMMIT。``F()`` 的版本在 READ COMMITTED 下會重讀
        最新的已提交值，於是第二次 `start()` 之後是 3（1 → 另一個 worker 加成 2 → 我們加成 3）；
        read-modify-write 則會拿手上的舊值寫回 2，另一個 worker 的那一次就這樣不見了。
        """
        doc_id = two_tenants_with_content["doc_a"]

        def _bump_on_another_connection(job_id: uuid.UUID) -> None:
            with connections["admin"].cursor() as cursor:
                # owner 也受 policy 管（FORCE RLS），所以這條連線同樣要有租戶參數。
                cursor.execute("SELECT set_config('app.tenant_id', %s, false)", [str(TENANT_A)])
                try:
                    cursor.execute(
                        "UPDATE knowledge_etljob SET attempt = attempt + 1 WHERE id = %s",
                        [str(job_id)],
                    )
                finally:
                    cursor.execute("SELECT set_config('app.tenant_id', '', false)")

        with tenant_context(TENANT_A), unit_of_work():
            created = EtlJobRepository().start(doc_id=doc_id, doc_version=1, stage="extract")

        assert created.attempt == 1

        original = EtlJob.objects.get_or_create

        def _read_then_let_the_other_worker_win(**kwargs: Any) -> tuple[EtlJob, bool]:
            job, was_created = original(**kwargs)
            _bump_on_another_connection(job.id)
            return job, was_created

        with (
            mock.patch.object(
                EtlJob.objects, "get_or_create", side_effect=_read_then_let_the_other_worker_win
            ),
            tenant_context(TENANT_A),
            unit_of_work(),
        ):
            job = EtlJobRepository().start(doc_id=doc_id, doc_version=1, stage="extract")

        assert job.attempt == 3, "另一個 worker 的那一次被蓋掉了——attempt 少算 = 重試上限失效"

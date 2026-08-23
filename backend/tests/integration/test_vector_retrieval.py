"""驗收：純向量檢索（06 §3·§3.1、05 §4、11 §2、13 §3 工作包 1C-4）。

這是**讀路徑的第一段**。1C-3 把 chunk 變成向量，這裡拿問題去把最相關的 chunk 找回來
——1D 的問答與引用整個站在它上面。

放 integration 而不是 unit，因為要驗的東西**全部**是與 DB 的互動：相似度由 pgvector
算、隔離由 RLS 擋、排序由 HNSW 索引決定。用假物件驗這一層等於什麼都沒驗。

四件事錯了都不會有錯誤訊息，而且症狀一模一樣（「答得很爛」）：

1. **索引真的被用到**。`vector_cosine_ops` 套在 halfvec 欄位上不會報錯，索引只是永遠
   不會被選用——檢索從數十毫秒退化成整表掃描，而結果完全正確。資料量小的時候連慢都
   感覺不到，等到有感時已經沒有人記得這裡有個選擇。
2. **過濾條件一個都不能少**：租戶、KB、`superseded`、以及 model + embedding_version。
   少任何一個，回來的 chunk 都「確實存在且看起來合理」——包括別的租戶的。
3. **分數的方向**。相似度（越大越相關）與距離（越小越相關）差一個負號，而兩者都會排
   出一個看起來像答案的清單。06 §3.1 的 rerank 門檻 0.3 是**相似度**，方向反了會讓
   1D 把最不相關的六筆餵給 LLM。
4. **引用需要的欄位要一路帶回來**（document_id、page、heading_path）。1D 要靠它們說出
   「這句話出自哪份文件第幾頁」，而那時已經離開這一層，補不回來。

**本檔不驗檢索品質。** MockProvider 的向量由 SHA-256 決定，具備決定性與相異性，但
**沒有語意相似性**——「請假」不會靠近「休假」。因此這裡驗的是機制（同樣的文字一定
找得回自己、排序方向對、過濾對），品質評測要等真模型與 Phase 2 的 golden set。
"""

from __future__ import annotations

import uuid

import pytest
from django.db import connection

from ai.gateway import AIGateway
from ai.gateway.providers.mock import MockEmbeddingProvider
from core.exceptions import NotFoundError
from repositories.knowledge import EmbeddingRepository
from services.knowledge.embedding import EmbeddingService
from services.rag.retrieval import RetrievalService, default_top_k
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_chunk, make_document, make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)

# 三段內容彼此無關——MockProvider 沒有語意相似性，所以「相關」在本檔的定義只有一種：
# 查詢字串與 chunk 內容**完全相同**（同樣的文字 → 同樣的向量 → 距離 0）。
_CONTENTS = (
    "第一段：員工請假應於三日前提出申請",
    "第二段：出差旅費以實報實銷為原則",
    "第三段：年度考核於每年十二月進行",
)


def _gateway() -> AIGateway:
    return AIGateway(embedding_provider=MockEmbeddingProvider(), retry_backoff_seconds=())


def _service() -> RetrievalService:
    return RetrievalService(gateway=_gateway())


@pytest.fixture
def tenants() -> None:
    for tenant_id, name in ((TENANT_A, "a"), (TENANT_B, "b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=f"tenant-{name}")


def _kb_with_vectors(
    tenant_id: uuid.UUID,
    *,
    contents: tuple[str, ...] = _CONTENTS,
    embed: bool = True,
    **kb_fields: object,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """建一個 KB、一份文件、幾個 chunk，並實際算出向量。

    **走真的 `EmbeddingService`** 而不是自己塞 Embedding 列：那條寫路徑決定了向量的
    model 與 version 是什麼，而檢索必須用同一組值去找。兩邊各自寫死的話，這裡會全綠
    而正式環境查不到任何東西——那正是最該被這一層擋下來的錯誤。
    """
    with tenant_scope(tenant_id):
        kb = make_knowledge_base(tenant_id=tenant_id, **kb_fields)
        document = make_document(kb=kb, status="chunked")
        chunk_ids = [
            make_chunk(
                document=document,
                seq=seq,
                content=content,
                meta={"page": seq + 1, "heading_path": ["第一章", f"第{seq + 1}節"]},
            ).id
            for seq, content in enumerate(contents)
        ]
    if embed:
        EmbeddingService(gateway=_gateway()).embed_document(tenant_id, document.id)
    return uuid.UUID(str(kb.id)), [uuid.UUID(str(c)) for c in chunk_ids]


class TestRanking:
    def test_the_exact_text_comes_back_first(self, tenants: None) -> None:
        """用一段 chunk 的原文去查，那一段必須排第一。

        這是 MockProvider 之下唯一驗得到的「相關性」（同文字 → 同向量 → 距離 0），
        但它同時驗到了整條鏈：查詢也走 Gateway、用同一個模型、算出可比對的向量。
        任何一環用錯模型，這條就會紅。
        """
        kb_id, chunk_ids = _kb_with_vectors(TENANT_A)

        results = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[1])

        assert results
        assert results[0].chunk_id == chunk_ids[1]

    def test_scores_are_similarities_not_distances(self, tenants: None) -> None:
        """分數是**相似度**：越大越相關，範圍 -1~1。

        距離與相似度差一個負號，而兩者都會排出一個看起來像答案的清單。06 §3.1 的
        rerank 門檻 0.3 是相似度——方向反了，1D 會把最不相關的六筆餵給 LLM，而回答
        看起來只是「品質不好」。

        **量的是 repository 而不是 service**（2B-2 起）：service 的回傳值經過 RRF
        融合，分數已經換成名次倒數和（第一名 1/61）。餘弦相似度的性質只在這一層還
        看得到，而它仍然要有人守——1D 的門檻與 2B-3 的絕對門檻都建立在它上面。
        """
        kb_id, chunk_ids = _kb_with_vectors(TENANT_A)
        embedded = _gateway().embed([_CONTENTS[0]], model="text-embedding-3-small")

        with tenant_scope(TENANT_A):
            hits = EmbeddingRepository().search(
                embedded.vectors[0],
                kb_id=kb_id,
                model=embedded.model,
                embedding_version=1,
                top_k=10,
                ef_search=80,
            )

        assert [hit.chunk_id for hit in hits][:1] == chunk_ids[:1]
        scores = [hit.score for hit in hits]
        assert scores == sorted(scores, reverse=True), "分數必須由大到小"
        # **範圍是 -1~1 而不是 0~1**：cosine 距離的範圍是 0~2（夾角 0°~180°），
        # 相似度 = 1 - 距離。夾角超過 90° 就是負的，那是「意思相反」而不是錯誤。
        # 寫成 0~1 的話，第一個真的不相關的查詢就會踩到，而那時看起來像分數算錯。
        assert all(-1.0 <= score <= 1.0 for score in scores)
        # 完全相同的文字 → 距離 0 → 相似度 1。halfvec 是 fp16，留 0.01 的容差。
        assert hits[0].score > 0.99

    def test_citation_fields_survive(self, tenants: None) -> None:
        """1D 的引用要說出「哪份文件、第幾頁、哪一節」。

        這是那些欄位最後一次還在手上——再往下就進了 prompt，補不回來。
        """
        kb_id, _ = _kb_with_vectors(TENANT_A)

        top = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[2])[0]

        assert top.document_id is not None
        assert top.content == _CONTENTS[2]
        assert top.page == 3
        assert top.heading_path == ["第一章", "第3節"]


class TestFilters:
    def test_another_knowledge_base_is_not_mixed_in(self, tenants: None) -> None:
        """檢索的範圍是**一個 KB**。

        漏掉 kb 條件的話，答案會引用到另一個知識庫的內容——而那些內容確實屬於這個
        租戶，所以沒有任何權限檢查會擋下來，使用者只會覺得「它在亂答」。
        """
        kb_a, _ = _kb_with_vectors(TENANT_A)
        _, other_ids = _kb_with_vectors(TENANT_A, contents=("完全不同的另一個知識庫內容",))

        results = _service().query(TENANT_A, kb_id=kb_a, query="完全不同的另一個知識庫內容")

        assert all(result.chunk_id not in other_ids for result in results)

    def test_superseded_chunks_never_surface(self, tenants: None) -> None:
        """re-ingest 之後的舊版 chunk 不得被檢索到。

        它們的向量還在（清理 job 屬 2A），內容也看起來完全合理——但那是文件的**舊
        版本**，拿它回答等於引用一份已經被改掉的文件，而引用連結會指向現在的版本。
        """
        from apps.knowledge.models import Chunk

        kb_id, chunk_ids = _kb_with_vectors(TENANT_A)
        with tenant_scope(TENANT_A):
            Chunk.objects.filter(id=chunk_ids[0]).update(superseded=True)

        results = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0])

        assert all(result.chunk_id != chunk_ids[0] for result in results)

    def test_only_the_current_model_and_version_are_used(self, tenants: None) -> None:
        """向量要用 KB 目前的 model + embedding_version 去找（06 §2.2）。

        重嵌入期間同一個 chunk 會有兩份向量。拿舊版的去比對新版的查詢向量，結果是
        兩個不同模型的向量空間混在一起——距離數字照樣算得出來，而排序完全沒有意義。
        """
        from apps.knowledge.models import Embedding

        kb_id, chunk_ids = _kb_with_vectors(TENANT_A)
        with tenant_scope(TENANT_A):
            # 把既有向量改標成「別的模型」——目前版本因此一筆都不剩。
            Embedding.objects.filter(chunk_id__in=chunk_ids).update(model="another-model")

        # 同上一條：驗的是向量那一路的過濾條件，因此明確走 `vector`。
        results = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0], mode="vector")

        assert results == []

    def test_chunks_without_a_vector_are_skipped(self, tenants: None) -> None:
        """embedding 還沒算完的 chunk 不該出現（也不該讓查詢失敗）。

        文件在 `chunked` 與 `ready` 之間有一段時間是這個狀態，而那段時間查詢仍要能
        服務——回傳「目前算得出來的那些」，而不是報錯或回空。
        """
        kb_id, _ = _kb_with_vectors(TENANT_A, embed=False)

        # **明確指定 `vector`**：2B-2 之後預設是 hybrid，而字面比對照樣找得到這些
        # chunk（它們有內容、只是還沒有向量）。那不是錯誤——re-embedding 期間由 FTS
        # 頂住是 hybrid 的紅利之一——但這條驗的是**向量那一路**的過濾條件。
        results = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0], mode="vector")

        assert results == []


class TestTopK:
    def test_the_default_is_forty(self, tenants: None) -> None:
        """06 §3.1：vector search top_k=40。

        **來源只有一個**（1D-5 起是 `services/rag/params.py`，見 15 §4.1）——1D 的
        問答與 `/rag/query` 要拿到同一組候選，否則「除錯用的 API 查得到、實際問答
        查不到」會變成一種可能，而那時沒有人會想到去比對兩個預設值。
        """
        assert default_top_k() == 40

    def test_it_limits_the_number_of_results(self, tenants: None) -> None:
        kb_id, _ = _kb_with_vectors(TENANT_A)

        results = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0], top_k=2)

        assert len(results) == 2

    def test_it_never_returns_more_than_exists(self, tenants: None) -> None:
        """候選比 top_k 少是正常情況，不是錯誤。"""
        kb_id, chunk_ids = _kb_with_vectors(TENANT_A)

        results = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0], top_k=100)

        assert len(results) == len(chunk_ids)


class TestIndexIsActuallyUsable:
    def test_the_hnsw_index_can_serve_this_ordering(self, tenants: None) -> None:
        """**這一條是本檔最重要的守門**：ops 類別與參數型別對得上。

        `vector_cosine_ops` 套在 halfvec 欄位上、或查詢參數沒轉成 halfvec（傳一般的
        list 會被當成 `vector`），都**不會報錯**——索引只是永遠無法服務這個
        ``ORDER BY``，檢索安靜地退化成排序整批候選。結果完全正確，所以沒有任何測試
        會紅，直到資料長大之後有人問「怎麼越來越慢」。

        **三個 planner 旋鈕全關**，因為這裡要問的是「能不能用」而不是「會不會用」：
        RLS 會在每個查詢加上 ``tenant_id = ...`` 這個條件，而 `ix_embedding_tenant_chunk`
        剛好服務得了它。測試資料只有幾列時，先用租戶索引篩再排序**本來就比較便宜**，
        planner 選它是對的。不關的話，這條測試驗到的是「資料很少」。

        **它證明的是相容性，不是效能**：正式環境會不會選 HNSW 取決於資料量與選擇度，
        那屬 11 §2 的調校與 Phase 2 的評測，不是單元層驗得到的東西。
        """
        _kb_with_vectors(TENANT_A)
        embedded = MockEmbeddingProvider().embed(
            [_CONTENTS[0]], model="mock-embedding", timeout_seconds=5.0
        )
        literal = "[" + ",".join(str(value) for value in embedded.vectors[0]) + "]"

        with tenant_scope(TENANT_A), connection.cursor() as cursor:
            for knob in ("enable_seqscan", "enable_bitmapscan", "enable_indexscan"):
                cursor.execute(f"SET LOCAL {knob} = off")
            # S608：literal 由上一行的 MockProvider 就地產生，沒有任何外部輸入。
            # EXPLAIN 也不能用參數化——planner 要看到實際的值才選得出索引。
            cursor.execute(
                "EXPLAIN SELECT id FROM knowledge_embedding "  # noqa: S608
                f"ORDER BY vector <=> '{literal}'::halfvec LIMIT 10"
            )
            plan = "\n".join(row[0] for row in cursor.fetchall())

        assert "ix_embedding_vector_hnsw" in plan, (
            f"HNSW 索引無法服務這個 ORDER BY——ops 類別或參數型別對不上：\n{plan}"
        )

    def test_ef_search_is_applied(self, tenants: None) -> None:
        """11 §2：`ef_search=80` 起步。

        它是「找多仔細」的旋鈕，預設值（40）比文件定的低——不設的話召回會比評測時
        差一截，而那個差距不會出現在任何錯誤訊息裡，只會讓答案偶爾少引用一份文件。

        用 `SET LOCAL`（交易區域）而不是連線層級：連線來自 pool，設在連線上會外溢到
        後面所有查詢，包括 ETL 那些完全不碰向量的。
        """
        from services.rag.retrieval import EF_SEARCH

        kb_id, _ = _kb_with_vectors(TENANT_A)
        service = _service()

        with tenant_scope(TENANT_A):
            service.query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0])
            with connection.cursor() as cursor:
                cursor.execute("SHOW hnsw.ef_search")
                applied = cursor.fetchone()[0]

        assert int(applied) == EF_SEARCH == 80

    def test_searching_outside_a_transaction_fails_loudly(self, tenants: None) -> None:
        """交易外的 ``SET LOCAL`` 只發一則 WARNING——**設定不生效，查詢照跑**。

        於是 ef_search 悄悄退回 PostgreSQL 的預設值（40），召回率下降而結果依然看起來
        完全正常。上一條測試證明的是「在交易內時它有生效」，這條釘住的是那個前提本身：
        呼叫端一旦跑到交易外，要當場失敗而不是安靜地變差。
        """
        from repositories.knowledge import EmbeddingRepository

        # 參數不需要對得上真實資料：守門在方法開頭，任何一次交易外的呼叫都到不了查詢。
        with pytest.raises(RuntimeError, match="交易內"):
            EmbeddingRepository().search(
                [0.0],
                kb_id=uuid.uuid4(),
                model="mock",
                embedding_version=1,
                top_k=5,
                ef_search=80,
            )


class TestTenantIsolation:
    def test_another_tenants_chunks_never_surface(self, tenants: None) -> None:
        """兩道防線（Repository filter + RLS）在這裡是同一件事的兩層。

        檢索是**唯一一條把別人的內容直接讀出來、再交給 LLM 講出口**的路徑。漏了條件
        的話，受害租戶看不到（資料在別人畫面上），得利租戶不知道（他以為那是自己的
        文件）——沒有人會回報。
        """
        _, foreign_ids = _kb_with_vectors(TENANT_B)
        kb_a, _ = _kb_with_vectors(TENANT_A)

        results = _service().query(TENANT_A, kb_id=kb_a, query=_CONTENTS[0])

        assert all(result.chunk_id not in foreign_ids for result in results)

    def test_another_tenants_kb_is_not_found(self, tenants: None) -> None:
        """跨租戶的 kb_id → `NotFoundError`（09 §2.3 的資源類規則）。

        回空清單是更糟的選擇：那等於承認「這個 id 存在但沒有東西」，而 404 連存在與
        否都不透露。
        """
        kb_b, _ = _kb_with_vectors(TENANT_B)

        with pytest.raises(NotFoundError):
            _service().query(TENANT_A, kb_id=kb_b, query=_CONTENTS[0])


class TestEdgeCases:
    def test_an_empty_knowledge_base_returns_nothing(self, tenants: None) -> None:
        """一個字都還沒上傳的 KB → 空清單，不是錯誤。

        1D 對「檢索不到」有自己的處置（06 §3.1：回「知識庫無相關內容」而不是硬答），
        那需要一個正常的空回應，不是一個例外。
        """
        with tenant_scope(TENANT_A):
            kb = make_knowledge_base(tenant_id=TENANT_A)

        results = _service().query(TENANT_A, kb_id=uuid.UUID(str(kb.id)), query="任何問題")

        assert results == []

    def test_a_blank_query_is_rejected(self, tenants: None) -> None:
        """空白查詢當場拒絕，不要送去算 embedding。

        provider 對空字串的行為各家不同（有的回錯、有的回一個沒有意義的向量），而後者
        會檢索出一組隨機的 chunk——看起來像答案，實際上與問題無關。
        """
        kb_id, _ = _kb_with_vectors(TENANT_A)

        with pytest.raises(ValueError):
            _service().query(TENANT_A, kb_id=kb_id, query="   ")

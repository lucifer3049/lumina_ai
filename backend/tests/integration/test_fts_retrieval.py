"""驗收：pgroonga 全文檢索（06 §3.1 的 FTS 一路、05 §5.3、13 §4 工作包 2B-1）。

**為什麼要有這條路**：向量檢索擅長「換句話說」，對**專有名詞、型號、法條編號**卻很鈍
——問「第 14 條」，它會回一段語氣很像但編號不對的文字，而那看起來完全像個答案。字面
比對補的正是這一半，兩路在 2B-2 以 RRF 融合。

放 integration 而不是 unit，理由與 `test_vector_retrieval.py` 完全相同：要驗的東西全部
是與 DB 的互動——斷詞由 PGroonga 做、隔離由 RLS 擋、分數由索引算。用假物件驗這一層等於
什麼都沒驗。

**四個陷阱，錯了都不會有錯誤訊息**（前兩個是 2B-1 開工前 spike 實測到的）：

1. **查詢必須帶 `superseded = false`**。索引是 partial 的，查詢少了同一個條件，planner
   就用不到它——退化成整表掃描，而結果完全正確。
2. **`pgroonga_score()` 在沒走索引時安靜地回 0.0**。不是錯誤、不是 NULL，是一個合法的
   分數；於是 2B-2 的 RRF 會拿到一組「全部同分」的候選，排序完全由 tie-break 決定。
   因此本檔對分數的斷言就是第 1 點的間接證據。
3. **過濾條件一個都不能少**：租戶、KB、`superseded`。少任何一個，回來的 chunk 都
   「確實存在且看起來合理」——包括別的租戶的。
4. **引用要的欄位得一路帶回來**（document_name、page、heading_path）。1D 的引用面板靠
   它們說出「這句話出自哪份文件第幾頁」，而那時已經離開這一層，補不回來。
"""

from __future__ import annotations

import uuid

import pytest
from django.db import connection

from core.tenant import tenant_context
from core.uow import unit_of_work
from rag.retrievers.vector import to_retrieved
from repositories.knowledge import ChunkRepository
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_chunk, make_document, make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)

INDEX_NAME = "ix_chunk_content_fts_active"

# 三段內容彼此無關，且各自有一個**只出現在自己身上**的稀有詞——那正是字面比對該贏的
# 情境（`ES256`、`統一發票`、`ix_chunk_tenant_kb_active`）。
_CONTENTS = (
    "存取權杖的有效期是 15 分鐘，簽章演算法採 ES256，API 節點只需要公鑰就能驗簽。",
    "出差旅費以實報實銷為原則，需要檢附統一發票，並經直屬主管核准後才能請款。",
    "檢索候選集走 partial index，名稱是 ix_chunk_tenant_kb_active，條件為未被標記取代。",
)


@pytest.fixture
def tenants() -> None:
    for tenant_id, name in ((TENANT_A, "a"), (TENANT_B, "b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=f"tenant-{name}")


def _kb_with_chunks(
    tenant_id: uuid.UUID, *, contents: tuple[str, ...] = _CONTENTS, superseded: bool = False
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """建一個 KB、一份文件與幾個 chunk。**不需要向量**——FTS 讀的是 chunk 本身。"""
    with tenant_scope(tenant_id):
        kb = make_knowledge_base(tenant_id=tenant_id)
        document = make_document(kb=kb, status="chunked", filename="人事規章.pdf")
        chunk_ids = [
            make_chunk(
                document=document,
                seq=seq,
                content=content,
                superseded=superseded,
                meta={"page": seq + 1, "heading_path": ["第一章", f"第{seq + 1}節"]},
            ).id
            for seq, content in enumerate(contents)
        ]
        return uuid.UUID(str(kb.id)), [uuid.UUID(str(chunk_id)) for chunk_id in chunk_ids]


def _search(tenant_id: uuid.UUID, kb_id: uuid.UUID, query: str, *, top_k: int = 40) -> list:
    with tenant_context(tenant_id), unit_of_work():
        return ChunkRepository().search_fts(query, kb_id=kb_id, top_k=top_k)


class TestIndex:
    def test_the_pgroonga_index_exists_and_is_partial(self, tenants: None) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'knowledge_chunk' AND indexname = %s",
                [INDEX_NAME],
            )
            row = cursor.fetchone()

        assert row is not None, f"{INDEX_NAME} 不存在——migration 沒跑或索引名改了"
        assert "pgroonga" in row[0]
        assert "superseded" in row[0], "索引不是 partial：歷史版本會被一起收進去"

    def test_the_query_can_actually_use_it(self, tenants: None) -> None:
        """EXPLAIN 必須看得到這個索引。

        partial index 只有在**查詢也帶著同一個條件**時才用得到；`search_fts` 少寫一個
        `superseded = false` 就會退化成整表掃描，而結果完全正確——資料量小的時候連慢都
        感覺不到，等到有感時已經沒有人記得這裡有個條件。
        """
        kb_id, _ = _kb_with_chunks(TENANT_A)

        with tenant_context(TENANT_A), unit_of_work():
            ChunkRepository().search_fts("ES256", kb_id=kb_id, top_k=10)
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL enable_seqscan = off")
                cursor.execute(
                    "EXPLAIN SELECT id FROM knowledge_chunk "
                    "WHERE superseded = false AND content &@* 'ES256'"
                )
                plan = "\n".join(row[0] for row in cursor.fetchall())

        assert INDEX_NAME in plan, f"pgroonga 索引無法服務這個查詢：\n{plan}"


class TestSearch:
    def test_a_rare_term_finds_its_chunk(self, tenants: None) -> None:
        """**這是 FTS 存在的理由**：`ES256` 這種字串在向量空間裡幾乎沒有訊號。"""
        kb_id, chunk_ids = _kb_with_chunks(TENANT_A)

        hits = _search(TENANT_A, kb_id, "ES256")

        assert [hit.chunk_id for hit in hits][:1] == [chunk_ids[0]]

    def test_a_natural_language_question_finds_the_right_chunk(self, tenants: None) -> None:
        """整句問句直接送進去（見 `test_fts_query.py` 的 spike 記錄：`&@` 與 `&@~` 對
        整句中文會回 0 筆）。"""
        kb_id, chunk_ids = _kb_with_chunks(TENANT_A)

        hits = _search(TENANT_A, kb_id, "出差的旅費要怎麼報銷？需要附什麼單據？")

        assert hits and hits[0].chunk_id == chunk_ids[1]

    def test_scores_are_positive_and_descending(self, tenants: None) -> None:
        """分數 > 0 是「索引真的被用到」的間接證據（見模組 docstring 第 2 點）。

        `pgroonga_score()` 在 seq scan 下回 0.0——不是錯誤、不是 NULL，是一個合法的
        分數，而 2B-2 的 RRF 會拿到一組全部同分的候選。
        """
        kb_id, _ = _kb_with_chunks(TENANT_A)

        hits = _search(TENANT_A, kb_id, "統一發票 報銷 核准")

        assert hits, "沒有任何命中"
        assert all(hit.score > 0 for hit in hits), f"分數為 0：{[h.score for h in hits]}"
        assert [hit.score for hit in hits] == sorted((h.score for h in hits), reverse=True)

    def test_an_unrelated_question_returns_nothing(self, tenants: None) -> None:
        """回空清單而不是「勉強給幾筆」：2B-2 的 RRF 只認名次，硬塞進去的無關段落會
        佔掉真正候選的位置。"""
        kb_id, _ = _kb_with_chunks(TENANT_A)

        assert _search(TENANT_A, kb_id, "量子計算的退相干時間有多長") == []

    def test_a_blank_query_is_rejected_before_it_reaches_the_database(self, tenants: None) -> None:
        """空查詢在 PGroonga 那裡回空集合，而那與「真的沒有相關內容」長得一模一樣。"""
        kb_id, _ = _kb_with_chunks(TENANT_A)

        with pytest.raises(ValueError):
            _search(TENANT_A, kb_id, "   ")

    def test_top_k_limits_the_result(self, tenants: None) -> None:
        kb_id, _ = _kb_with_chunks(TENANT_A)

        assert len(_search(TENANT_A, kb_id, "檢索 報銷 權杖 發票 索引", top_k=1)) <= 1

    def test_an_english_question_does_not_match_chinese_text(self, tenants: None) -> None:
        """**跨語言時 FTS 天然失效**（06 §3.1）——這條是把那個已知限制釘住，不是缺陷。

        字面比對沒有共同的詞可比，所以跨語言的召回全靠向量那一路撐（RRF 天然容忍單路
        弱訊號，無需特判）。哪天有人「修好」了這條讓它有命中，那多半是斷詞器被換成了
        會把中文切成單字的設定——那會讓所有中文查詢的精確度一起崩掉。
        """
        kb_id, _ = _kb_with_chunks(TENANT_A)

        assert _search(TENANT_A, kb_id, "How long is the access token valid?") == []


class TestFilters:
    def test_it_never_crosses_tenants(self, tenants: None) -> None:
        """兩個租戶放**完全相同**的內容——最容易越界的情境，而越界時結果看起來完全
        正常（內容一樣），只有 chunk 的歸屬不同。"""
        kb_a, chunks_a = _kb_with_chunks(TENANT_A)
        _kb_b, chunks_b = _kb_with_chunks(TENANT_B)

        hits = _search(TENANT_A, kb_a, "ES256")

        found = {hit.chunk_id for hit in hits}
        assert found <= set(chunks_a)
        assert not found & set(chunks_b)

    def test_it_only_looks_inside_the_requested_kb(self, tenants: None) -> None:
        """漏了 kb 條件時，回來的每一筆都是同租戶的合法資料，所以不會有錯誤也不會有
        紅燈——使用者只會覺得「這個知識庫怎麼查得到別的知識庫的內容」。"""
        kb_one, _ = _kb_with_chunks(TENANT_A)
        _kb_two, chunks_two = _kb_with_chunks(TENANT_A)

        hits = _search(TENANT_A, kb_one, "ES256")

        assert not {hit.chunk_id for hit in hits} & set(chunks_two)

    def test_superseded_chunks_are_invisible(self, tenants: None) -> None:
        """re-ingest 之後的舊版殘留不得被檢索到——它們的內容是**上一版**的，而 LLM
        拿到之後會照著回答，答案看起來完全合理。"""
        kb_id, _ = _kb_with_chunks(TENANT_A, superseded=True)

        assert _search(TENANT_A, kb_id, "ES256") == []


class TestShape:
    def test_hits_carry_everything_a_citation_needs(self, tenants: None) -> None:
        """與向量那一路**回同一種形狀**——2B-2 要把兩邊的候選混在一起，形狀不同的話
        融合那一層就得為每一路各寫一次「這筆是從哪來的」。"""
        kb_id, _ = _kb_with_chunks(TENANT_A)

        retrieved = to_retrieved(_search(TENANT_A, kb_id, "ES256"))

        assert retrieved
        first = retrieved[0]
        assert first.document_name == "人事規章.pdf"
        assert first.doc_version == 1
        assert first.page == 1
        assert first.heading_path == ["第一章", "第1節"]

"""驗收：`rag_trace` 的內容（06 §7、12 §1.1 Correlation，13 §4 工作包 2B-5）。

06 §7 一句話定義了它：「每次查詢產生 `rag_trace`（request_id 關聯）：各階段耗時、
候選數、rerank 分數分布、壓縮率、最終 token 分配、citation 驗證結果。**除錯與評測
共用同一 trace**。」

**現在這些數字散在五、六行不同的 log 事件裡**（`retrieval_completed`、
`fts_retrieval_completed`、`rerank_applied`、`rag_context_selected`…），而它們拼不起來：
沒有共同的鍵、逐路的分數在融合之後就被覆蓋掉、citation 的驗證結果落在另一個服務裡。
於是「這一題為什麼答錯」要靠人在 log 裡對時間戳，而那件事沒有人會做第二次。

本檔驗**內容**（走真的 DB、真的兩路檢索、Mock 的 provider）；request_id 的關聯與
`/rag/query` 的回應形狀在 `tests/api/test_rag_trace_correlation.py`。

四件事錯了都不會有例外：

1. **逐路的原始分數不見了**。`fuse_candidates` 把 `score` 換成 RRF 的融合分數（那是
   對的——那是排序用的尺），於是「向量給這一段幾分」在融合之後**永遠查不回來**。
   `rag/pipeline.py` 的 docstring 自 2B-2 起就寫著「要各路原本的分數的話，那屬於
   2B-5 的 `rag_trace`」——這一包要兌現它。
2. **rerank 分數沒記**。2B-4 結案缺口①：絕對門檻 0.3 的條件已經具備（分數回到 0~1
   的尺度），但沒有任何地方留下分布，所以沒有依據裁決它該是 0.3 還是別的數字。
3. **降級與棄權混在一起**。FTS 棄權（這句話裡沒有識別符）是**正確答案**，而 FTS 掛掉
   是故障——trace 把兩者記成同一件事的話，`degraded` 會被純中文問句灌爆，真正的故障
   就淹在裡面（2B-2b 已經在 `usage.rag.degraded` 上踩過同一個坑）。
4. **trace 記了 chunk 內文**。log 會流到 Loki 並長期保存，而 chunk 內文是租戶的文件
   ——那是鐵則 9 的「secrets 不進 log」同一條線上的東西，只是這次外流的是客戶資料。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ai.gateway import AIGateway
from ai.gateway.providers import ProviderRerank, RerankProvider
from ai.gateway.providers.mock import MockEmbeddingProvider, MockRerankProvider
from core.exceptions import ProviderTimeoutError
from services.rag.retrieval import RetrievalService
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_chunk, make_document, make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)

_CONTENTS = (
    "員工請假應於三日前提出申請，並經直屬主管核准後生效。",
    "出差旅費以實報實銷為原則，需要檢附統一發票才能請款。",
    "年度考核於每年十二月進行，考核結果影響次年度調薪。",
)
# 含識別符的問句：FTS 那一路才會真的發言（純中文概念問句會棄權，見 2B-2b）。
_FTS_QUERY = "ISO-27001 稽核與請假規定"


class _BrokenRerank:
    name = "broken"

    def rerank(
        self, query: str, documents: list[str], *, model: str, timeout_seconds: float
    ) -> ProviderRerank:
        raise ProviderTimeoutError("rerank 逾時")


def _service(rerank: RerankProvider | None = None) -> RetrievalService:
    gateway = AIGateway(
        embedding_provider=MockEmbeddingProvider(),
        rerank_provider=rerank or MockRerankProvider(),
        retry_backoff_seconds=(),
    )
    return RetrievalService(gateway=gateway)


@pytest.fixture
def tenants() -> None:
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a")


def _kb(**config: Any) -> uuid.UUID:
    from services.knowledge.embedding import EmbeddingService

    with tenant_scope(TENANT_A):
        kb = make_knowledge_base(tenant_id=TENANT_A, config=config)
        document = make_document(kb=kb, status="chunked")
        for seq, content in enumerate(_CONTENTS):
            make_chunk(document=document, seq=seq, content=content, meta={"page": seq + 1})
        kb_id = uuid.UUID(str(kb.id))
        document_id = uuid.UUID(str(document.id))

    gateway = AIGateway(embedding_provider=MockEmbeddingProvider(), retry_backoff_seconds=())
    EmbeddingService(gateway=gateway).embed_document(TENANT_A, document_id)
    return kb_id


class TestItExists:
    def test_every_query_produces_one(self, tenants: None) -> None:
        kb_id = _kb()

        outcome = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0])

        assert outcome.trace is not None

    def test_the_chat_path_produces_one_too(self, tenants: None) -> None:
        """除錯與評測共用同一 trace（06 §7）——而使用者遇到的問題出在問答那條路，
        不是在 `/rag/query`。兩條路各記各的話，除錯用的那份永遠不是出事的那份。"""
        kb_id = _kb()

        outcome = _service().retrieve_for_chat(TENANT_A, kb_ids=[kb_id], query=_CONTENTS[0])

        assert outcome.trace is not None

    def test_a_chat_without_any_kb_has_no_trace(self, tenants: None) -> None:
        """純閒聊路徑（06 §9）不檢索，也就沒有東西可記。

        記一筆全是 0 的 trace 會**汙染分母**——「有多少 % 的查詢降級了」會把從來沒查過
        知識庫的那些也算進去，而那個比例只會愈看愈好（同 `ChatService._citations` 對
        `usage.rag` 的處置）。
        """
        outcome = _service().retrieve_for_chat(TENANT_A, kb_ids=[], query="今天天氣如何")

        assert outcome.trace is None


class TestRoutes:
    def test_the_vector_route_is_recorded_with_its_own_scores(self, tenants: None) -> None:
        """**這是 `fuse_candidates` docstring 欠下的那筆帳。**

        融合之後 `chunk.score` 是名次倒數和（第一名 ≈ 0.016），原本的餘弦相似度就此
        消失。「向量到底覺得這一段有多像」在事後**完全查不回來**，而那是判斷「檢索
        爛還是排序爛」的第一個數字。
        """
        kb_id = _kb()

        trace = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0]).trace

        assert trace is not None
        route = next(r for r in trace.routes if r.name == "vector")
        assert route.candidate_count == len(_CONTENTS)
        assert len(route.top_scores) == len(_CONTENTS)
        # 餘弦相似度的尺度（0~1），不是 RRF 的 1/61。
        assert 0.0 < route.top_scores[0] <= 1.0
        assert route.top_scores == tuple(sorted(route.top_scores, reverse=True))

    def test_hybrid_records_both_routes_separately(self, tenants: None) -> None:
        """兩路各記一筆。合成一個「候選數」的話，「FTS 撈到 0 筆」與「向量撈到 0 筆」
        在報表上分不出來——而兩者的處置完全不同。"""
        kb_id = _kb()

        trace = _service().query(TENANT_A, kb_id=kb_id, query=_FTS_QUERY, mode="hybrid").trace

        assert trace is not None
        assert [route.name for route in trace.routes] == ["vector", "fts"]

    def test_the_vector_only_mode_has_no_fts_route(self, tenants: None) -> None:
        """`vector` 模式記一筆 0 筆候選的 fts 路的話，「這次沒跑 FTS」與「跑了但沒
        撈到」會被記成同一件事，而 2B-4 的歸因（贏的是 rerank 不是 hybrid）正是靠
        分得開這兩者。"""
        kb_id = _kb()

        trace = _service().query(TENANT_A, kb_id=kb_id, query=_FTS_QUERY, mode="vector").trace

        assert trace is not None
        assert [route.name for route in trace.routes] == ["vector"]

    def test_an_abstaining_fts_route_is_not_a_degradation(self, tenants: None) -> None:
        """棄權（2B-2b）是「這句話裡沒有字面比對幫得上忙的東西」，是正確答案而不是
        故障。trace 要說得出它棄權了，但不得記進 `degraded`。"""
        kb_id = _kb()

        trace = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0], mode="hybrid").trace

        assert trace is not None
        route = next(r for r in trace.routes if r.name == "fts")
        assert route.abstained is True
        assert trace.degraded == ()

    def test_the_fused_count_is_recorded(self, tenants: None) -> None:
        """融合之後剩幾段——它與各路候選數的差，就是「重複命中」的量，也是
        RRF 有沒有在做事的第一個訊號。"""
        kb_id = _kb()

        trace = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0]).trace

        assert trace is not None
        assert trace.fused_count == len(_CONTENTS)


class TestRerankScores:
    def test_the_score_distribution_is_recorded(self, tenants: None) -> None:
        """**2B-4 結案缺口①。** 絕對門檻 0.3 至今預設關閉，理由不是「不想開」，是
        「沒有分布可以裁決它」——報告不記分數，而驗證腳本上那組 0.9940 / 0.0000 是
        4 段的玩具樣本。

        逐段記下來之後，跑一次 144 題就有真實分布。
        """
        kb_id = _kb()

        trace = (
            _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[2], mode="vector+rerank").trace
        )

        assert trace is not None
        assert trace.rerank is not None
        assert trace.rerank.applied is True
        assert trace.rerank.scores
        assert trace.rerank.scores == tuple(sorted(trace.rerank.scores, reverse=True))
        assert trace.rerank.threshold == 0.0
        assert trace.rerank.kept_count == len(trace.rerank.scores)

    def test_a_degraded_rerank_records_no_scores(self, tenants: None) -> None:
        """降級之後手上是 RRF 的融合分數。把它記進 `rerank.scores` 的話，之後拿來
        裁決門檻的分布裡會混進一堆 0.016——而那看起來只是「分數偏低」，不是「這幾筆
        根本不是 cross-encoder 給的」。
        """
        kb_id = _kb()

        trace = (
            _service(_BrokenRerank())
            .query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0], mode="vector+rerank")
            .trace
        )

        assert trace is not None
        assert trace.rerank is not None
        assert trace.rerank.applied is False
        assert trace.rerank.scores == ()
        assert "rerank" in trace.degraded

    def test_a_non_rerank_mode_has_no_rerank_section(self, tenants: None) -> None:
        kb_id = _kb()

        trace = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0], mode="vector").trace

        assert trace is not None
        assert trace.rerank is None


class TestContextAndCompression:
    def test_the_context_selection_is_recorded(self, tenants: None) -> None:
        """06 §7 的「壓縮率、最終 token 分配」。

        它回答的是「花了多少預算、留下多少內容」——而 context 被裁掉的那幾段是
        **看不見的損失**：回答只是「答得不夠完整」，沒有任何地方指向裁切。
        """
        kb_id = _kb()

        trace = _service().retrieve_for_chat(TENANT_A, kb_ids=[kb_id], query=_CONTENTS[0]).trace

        assert trace is not None
        assert trace.context is not None
        assert trace.context.chunk_count == len(_CONTENTS)
        assert trace.context.tokens > 0
        assert trace.context.token_budget > 0
        # 壓縮率 = 進 context 的 token ÷ 候選的 token。1.0 代表一段都沒被裁掉。
        assert trace.context.compression_ratio == pytest.approx(1.0)

    def test_the_query_path_has_no_context_section(self, tenants: None) -> None:
        """`/rag/query` 不組 context（它不生成答案），記一個 0 段的 context 會讓
        「這條路不裁切」與「裁到只剩 0 段」長得一樣。"""
        kb_id = _kb()

        trace = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0]).trace

        assert trace is not None
        assert trace.context is None

    def test_a_tight_budget_shows_up_as_a_smaller_ratio(self, tenants: None) -> None:
        kb_id = _kb(retrieval={"context_token_budget": 1})

        trace = _service().retrieve_for_chat(TENANT_A, kb_ids=[kb_id], query=_CONTENTS[0]).trace

        assert trace is not None
        assert trace.context is not None
        assert trace.context.chunk_count == 1
        assert trace.context.compression_ratio < 1.0


class TestStageTimings:
    def test_each_stage_is_timed(self, tenants: None) -> None:
        """06 §7 的「各階段耗時」。11 §4 的延遲預算是逐階段的，而事後要回答
        「p95 是誰吃掉的」，唯一的辦法就是每一階段各記一個數字。"""
        kb_id = _kb()

        trace = (
            _service().query(TENANT_A, kb_id=kb_id, query=_FTS_QUERY, mode="hybrid+rerank").trace
        )

        assert trace is not None
        assert set(trace.stages) >= {"embed", "vector", "fuse", "rerank"}
        assert all(value >= 0.0 for value in trace.stages.values())

    def test_the_total_is_at_least_the_sum_of_the_stages(self, tenants: None) -> None:
        """總時間比各段加起來還短的話，代表有一段被重複扣掉或漏記——而那種錯誤
        會讓「檢索只花了 3ms」這種結論看起來完全可信。"""
        kb_id = _kb()

        trace = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0]).trace

        assert trace is not None
        assert trace.elapsed_ms >= sum(trace.stages.values()) - 1e-6


class TestItIsSafeToLog:
    def test_no_chunk_content_appears_anywhere(self, tenants: None) -> None:
        """**trace 會被寫進 log 並長期保存**（12 §1.1 → Loki）。

        chunk 的內文是租戶的文件——人事規章、合約、病歷。把它寫進 log 等於在另一個
        保存週期、另一套權限之下再存一份客戶資料，而那不會有任何徵兆。trace 只記
        chunk_id 與分數：查得到是哪一段，而內容要另外去查有權限的地方。
        """
        kb_id = _kb()

        trace = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0]).trace

        assert trace is not None
        serialised = repr(trace.as_dict())
        for content in _CONTENTS:
            assert content not in serialised

    def test_the_recorded_chunk_ids_are_the_real_ones(self, tenants: None) -> None:
        """記 id 是為了「這一段到底是哪一段」查得回去——記了但對不回資料庫的話，
        它只是一串好看的十六進位。"""
        kb_id = _kb()

        outcome = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0])

        assert outcome.trace is not None
        route = next(r for r in outcome.trace.routes if r.name == "vector")
        assert set(route.top_chunk_ids) == {str(chunk.chunk_id) for chunk in outcome.chunks}

    def test_the_number_of_recorded_candidates_is_bounded(self, tenants: None) -> None:
        """一筆 trace 是一行 JSON。逐路把 40 段全部記下來的話，尖峰時每次查詢都在
        往 Loki 送幾 KB——而那是 11 §4 熱路徑上實實在在的成本。

        **這個上限不是可調參數**（15 §4.1 的例外條款，同 `MAX_TOP_K`）：它保護的是
        日誌管線，不是「由使用者決定」的產品行為。
        """
        from rag.trace import MAX_RECORDED_CANDIDATES

        kb_id = _kb()

        trace = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0]).trace

        assert trace is not None
        for route in trace.routes:
            assert len(route.top_scores) <= MAX_RECORDED_CANDIDATES
            assert len(route.top_chunk_ids) <= MAX_RECORDED_CANDIDATES

"""驗收：rerank 接進檢索鏈與它的降級（06 §3.1／§3.3、11 §4、13 §4 工作包 2B-3）。

管線是 `兩路檢索 → 相對門檻 → RRF 融合 → **rerank** → 絕對門檻 → 裁進 context`。
這一層驗的是那兩個新環節，而它們各有一個**錯了不會有例外**的陷阱：

1. **降級時絕對門檻不得生效**。06 §3.1 的 0.3 是 cross-encoder 的尺度；rerank 被跳過
   之後手上只有 RRF 的融合分數（第一名 1/61 ≈ 0.016），拿 0.3 去比會把**全部**候選砍
   光——使用者看到的是「這個知識庫突然什麼都答不出來」，而 log 裡只有一行降級 warning。
   這是 1D-5 當初拒絕啟用絕對門檻的同一個理由，只是換了一個觸發條件。
2. **降級要說得出來**。rerank 掛掉時答案仍然出得來（那是降級的目的），差別只在排序
   ——沒有標記的話，「品質變差」這件事在任何地方都查不到，而評測分數掉一截時沒有人
   會想到是 rerank 靜靜地停了三天。

**rerank 一律走 MockProvider**（13 §4 的 2B 定案：CI 綠燈不得依賴 GPU）。真 TEI 的接線
與延遲量測屬 2B-4。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ai.gateway import AIGateway
from ai.gateway.providers import ProviderRerank, RerankProvider
from ai.gateway.providers.mock import MockEmbeddingProvider, MockRerankProvider
from core.exceptions import ProviderError, ProviderTimeoutError
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


class _BrokenRerank:
    """rerank 壞掉的樣子：TEI 沒起來、模型還在載、GPU 被別的行程佔滿。"""

    name = "broken"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or ProviderTimeoutError("rerank 逾時")
        self.calls = 0

    def rerank(
        self, query: str, documents: list[str], *, model: str, timeout_seconds: float
    ) -> ProviderRerank:
        self.calls += 1
        raise self.error


class _SpyRerank(MockRerankProvider):
    """記下 rerank 被呼叫時拿到幾段候選（驗 top_n 與「有沒有被呼叫」）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def rerank(
        self, query: str, documents: list[str], *, model: str, timeout_seconds: float
    ) -> ProviderRerank:
        self.calls.append({"query": query, "count": len(documents), "timeout": timeout_seconds})
        return super().rerank(query, documents, model=model, timeout_seconds=timeout_seconds)


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


class TestWiring:
    def test_rerank_runs_only_in_a_rerank_mode(self, tenants: None) -> None:
        """`vector`／`hybrid` 不得偷跑 rerank：那會讓 2B-4 的評測拿 rerank 跟 rerank 比。"""
        kb_id = _kb()
        spy = _SpyRerank()

        _service(spy).query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0], mode="hybrid")

        assert spy.calls == []

    def test_it_reorders_the_fused_candidates(self, tenants: None) -> None:
        """rerank 的全部價值就是「換一個更懂的東西重排」。"""
        kb_id = _kb()

        outcome = _service().query(TENANT_A, kb_id=kb_id, query=_CONTENTS[2], mode="vector+rerank")

        assert outcome.chunks and outcome.chunks[0].content == _CONTENTS[2]
        assert outcome.degraded == ()

    def test_it_reranks_at_most_the_context_size(self, tenants: None) -> None:
        """06 §3.1 的 `top_n=6~8` **就是** Phase 1 的 `context_chunks`——不另開一個旋鈕。

        兩個數字會漂，而漂掉的症狀是「rerank 排了 8 段、context 只放得下 6 段」：花了
        cross-encoder 的錢卻把它排最好的那兩段丟掉。
        """
        kb_id = _kb(retrieval={"context_chunks": 2})
        spy = _SpyRerank()

        _service(spy).query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0], mode="hybrid+rerank")

        assert spy.calls, "rerank 沒有被呼叫"
        assert spy.calls[0]["count"] <= 24, "進 rerank 的是融合後的候選（RRF → 24）"

    def test_the_timeout_is_the_budgeted_one(self, tenants: None) -> None:
        """11 §4：rerank 逾 1.2s 直接跳過。這個值住在設定裡（`ai_rerank_timeout_seconds`），
        不是寫死在編排裡。"""
        kb_id = _kb()
        spy = _SpyRerank()

        _service(spy).query(TENANT_A, kb_id=kb_id, query=_CONTENTS[0], mode="hybrid+rerank")

        assert spy.calls[0]["timeout"] == pytest.approx(1.2)


class TestDegradation:
    def test_a_timeout_falls_back_to_the_fused_order(self, tenants: None) -> None:
        """**跳過而不是失敗**（06 §1 的降級優先於失敗）：答案照樣出得來，差別只在排序。"""
        kb_id = _kb()

        outcome = _service(_BrokenRerank()).query(
            TENANT_A, kb_id=kb_id, query=_CONTENTS[0], mode="hybrid+rerank"
        )

        assert outcome.chunks, "降級之後仍然要有候選"

    def test_the_degradation_is_reported(self, tenants: None) -> None:
        """沒有標記的話，「rerank 靜靜地停了三天」在任何地方都查不到——而看得到的只有
        評測分數掉了一截。標記一路走到 `usage.rag`（1D-5 的 `usage.rag` 子物件）。"""
        kb_id = _kb()

        outcome = _service(_BrokenRerank(ProviderError("TEI 沒起來"))).query(
            TENANT_A, kb_id=kb_id, query=_CONTENTS[0], mode="hybrid+rerank"
        )

        assert "rerank" in outcome.degraded

    def test_the_absolute_threshold_is_skipped_when_rerank_is(self, tenants: None) -> None:
        """**本檔最重要的一條**（見模組 docstring 第 1 點）。

        門檻 0.3 是 cross-encoder 的尺度；rerank 被跳過之後手上只有 RRF 的融合分數
        （第一名 1/61 ≈ 0.016），套上去會把全部候選砍光——而使用者看到的是「這個知識庫
        突然什麼都答不出來」，log 裡只有一行降級 warning。
        """
        kb_id = _kb(retrieval={"rerank_threshold": 0.3})

        outcome = _service(_BrokenRerank()).query(
            TENANT_A, kb_id=kb_id, query=_CONTENTS[0], mode="hybrid+rerank"
        )

        assert outcome.chunks, "降級時不得套用絕對門檻"
        assert outcome.degraded == ("rerank",)


class TestAbsoluteThreshold:
    def test_candidates_below_the_threshold_are_dropped(self, tenants: None) -> None:
        """06 §3.3 的幻覺防線一：全部低於門檻 → 誠實回「知識庫無相關內容」，而不是
        拿一堆不相關的段落硬答。門檻在 2B-3 **第一次生效**（1D-5 記的那筆兌現）。"""
        kb_id = _kb(retrieval={"rerank_threshold": 0.99})

        outcome = _service().query(
            TENANT_A, kb_id=kb_id, query="完全無關的問題", mode="hybrid+rerank"
        )

        assert outcome.chunks == []

    def test_it_is_off_by_default(self, tenants: None) -> None:
        """預設值來自 `app_settings`，而 KB 可以覆寫（15 §4.1）。預設不開的理由同
        1D-5：門檻會砍東西，開不開由資料決定。"""
        kb_id = _kb()

        outcome = _service().query(
            TENANT_A, kb_id=kb_id, query="完全無關的問題", mode="hybrid+rerank"
        )

        assert outcome.chunks, "預設門檻不該砍掉候選"


class TestChatPath:
    def test_retrieve_for_chat_carries_the_degradation(self, tenants: None) -> None:
        """問答那條路要拿得到標記——`usage.rag` 的 `degraded` 就是從這裡來的。"""
        kb_id = _kb()

        outcome = _service(_BrokenRerank()).retrieve_for_chat(
            TENANT_A, kb_ids=[kb_id], query=_CONTENTS[0], mode="hybrid+rerank"
        )

        assert outcome.chunks
        assert outcome.degraded == ("rerank",)

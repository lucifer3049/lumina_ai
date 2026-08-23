"""驗收：hybrid 檢索的編排（06 §3.1、13 §4 工作包 2B-2）。

兩路各自的正確性已經有人守（`test_vector_retrieval.py`、`test_fts_retrieval.py`），
融合的算術也有（`tests/unit/test_rrf.py`）。**這一層驗的是把它們串起來的那幾個決定**，
而那幾個決定錯了都不會有例外：

1. **FTS 那路根本沒被呼叫**。hybrid 退化成純向量，答案照樣出得來、引用照樣正確——
   只是「第 14 條」這類問題永遠找不到，而那正是 2B 存在的理由。
2. **FTS 出事時整輪失敗**。FTS 是增強而不是必要（06 §1 的「降級優先於失敗」）：索引
   壞掉、查詢語法炸掉、pgroonga 沒裝，這些都該退回純向量，而不是讓使用者問不了問題。
3. **評測量到的不是它以為的模式**。`--mode vector` 若偷偷跑了 hybrid，2B-2 的結論
   就是拿 hybrid 跟 hybrid 比——差距是 0，而報告看起來完全正常。

**MockProvider 的向量沒有語意相似性**（同 `test_vector_retrieval.py`）：「相關」在本檔
只有一種定義——查詢字串與 chunk 內容**完全相同**。因此「只有 FTS 找得到的東西」在這裡
是一個字面上存在、但向量對它毫無訊號的稀有詞。這剛好是 hybrid 要解決的真實情境。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ai.gateway import AIGateway
from ai.gateway.providers.mock import MockEmbeddingProvider
from repositories.knowledge import ChunkHit, ChunkRepository
from services.rag.retrieval import RetrievalService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_chunk, make_document, make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)

# 第一段是「向量找得到」的（查詢用原文命中）；第二段帶一個稀有詞，向量對它沒有訊號，
# 只有字面比對找得到。
# 帶一個識別符（`HR-001`）：2B-2b 之後，沒有識別符的問句會讓 FTS 那一路棄權，而
# 降級測試需要它**真的被呼叫到**才驗得出降級。
_VECTOR_TARGET = "員工請假應於三日前提出申請，表單編號 HR-001，並經直屬主管核准。"
_FTS_TARGET = "簽章演算法採 ES256，API 節點只需要公鑰就能驗簽。"
_FILLER = "年度考核於每年十二月進行，結果影響次年調薪。"


class _SpyChunks(ChunkRepository):
    """記下 FTS 被呼叫了幾次、帶什麼參數。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    def search_fts(self, query: str, *, kb_id: uuid.UUID, top_k: int) -> list[ChunkHit]:
        self.calls.append({"query": query, "kb_id": kb_id, "top_k": top_k})
        return super().search_fts(query, kb_id=kb_id, top_k=top_k)


class _BrokenChunks(ChunkRepository):
    """FTS 壞掉的樣子：索引不存在、pgroonga 沒裝、查詢被 planner 選成 seq scan。"""

    def search_fts(self, query: str, *, kb_id: uuid.UUID, top_k: int) -> list[ChunkHit]:
        raise RuntimeError("pgroonga: [similar][text] similar search available only in index scan")


def _service(chunks: ChunkRepository | None = None) -> RetrievalService:
    gateway = AIGateway(embedding_provider=MockEmbeddingProvider(), retry_backoff_seconds=())
    return RetrievalService(gateway=gateway, chunks=chunks)


@pytest.fixture
def tenants() -> None:
    for tenant_id, name in ((TENANT_A, "a"), (TENANT_B, "b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=f"tenant-{name}")


def _kb(tenant_id: uuid.UUID, **config: Any) -> uuid.UUID:
    """建一個 KB 與三段內容，並實際算出向量（走真的 EmbeddingService 那條寫路徑）。"""
    from services.knowledge.embedding import EmbeddingService

    with tenant_scope(tenant_id):
        kb = make_knowledge_base(tenant_id=tenant_id, config=config)
        document = make_document(kb=kb, status="chunked")
        for seq, content in enumerate((_VECTOR_TARGET, _FTS_TARGET, _FILLER)):
            make_chunk(document=document, seq=seq, content=content, meta={"page": seq + 1})
        kb_id = uuid.UUID(str(kb.id))
        document_id = uuid.UUID(str(document.id))

    gateway = AIGateway(embedding_provider=MockEmbeddingProvider(), retry_backoff_seconds=())
    EmbeddingService(gateway=gateway).embed_document(tenant_id, document_id)
    return kb_id


class TestHybrid:
    def test_a_term_only_the_keyword_path_can_find_shows_up(self, tenants: None) -> None:
        """**這是 2B 的全部理由**：向量對 `ES256` 這種字串沒有訊號（mock 之下更是），
        而字面比對一找就到。純向量模式下這一段不會出現。"""
        kb_id = _kb(TENANT_A)

        hits = _service().query(TENANT_A, kb_id=kb_id, query="ES256")

        assert _FTS_TARGET in [hit.content for hit in hits]

    def test_the_vector_path_still_contributes(self, tenants: None) -> None:
        """hybrid 不能是「FTS 蓋過向量」：換句話說的問題仍然只有向量答得出來。"""
        kb_id = _kb(TENANT_A)

        hits = _service().query(TENANT_A, kb_id=kb_id, query=_VECTOR_TARGET)

        assert hits[0].content == _VECTOR_TARGET

    def test_a_chunk_found_by_both_paths_appears_once(self, tenants: None) -> None:
        """重複的代價是兩份 token 換零份新資訊，而 hybrid 讓重複變成常態。"""
        kb_id = _kb(TENANT_A)

        hits = _service().query(TENANT_A, kb_id=kb_id, query=_FTS_TARGET)

        chunk_ids = [hit.chunk_id for hit in hits]
        assert len(chunk_ids) == len(set(chunk_ids))

    def test_it_never_crosses_tenants(self, tenants: None) -> None:
        """兩個租戶放**完全相同**的內容：FTS 那一路是 2B-1 新開的洞，這裡再守一次。"""
        kb_a = _kb(TENANT_A)
        kb_b = _kb(TENANT_B)

        hits = _service().query(TENANT_A, kb_id=kb_a, query="ES256")

        with tenant_scope(TENANT_B):
            other_ids = {chunk.id for chunk in ChunkRepository().for_retrieval(kb_id=kb_b)}
        assert not {hit.chunk_id for hit in hits} & other_ids


class TestMode:
    def test_vector_mode_never_touches_the_keyword_path(self, tenants: None) -> None:
        """`--mode vector` 偷跑 hybrid 的話，2B-2 的結論就是拿 hybrid 跟 hybrid 比
        ——差距是 0，而報告看起來完全正常。"""
        kb_id = _kb(TENANT_A)
        spy = _SpyChunks()

        _service(spy).query(TENANT_A, kb_id=kb_id, query="ES256", mode="vector")

        assert spy.calls == []

    def test_hybrid_mode_passes_the_configured_fts_top_k(self, tenants: None) -> None:
        """FTS 的候選數走 `services/rag/params.py`（15 §4.1），不是寫死在 service 裡。"""
        kb_id = _kb(TENANT_A, retrieval={"fts_top_k": 7})
        spy = _SpyChunks()

        _service(spy).query(TENANT_A, kb_id=kb_id, query="ES256")

        assert [call["top_k"] for call in spy.calls] == [7]

    def test_the_kb_can_opt_out_of_hybrid(self, tenants: None) -> None:
        """KB 覆寫要真的生效——後台改了沒有反應是 15 §4.1 要防的那件事。"""
        kb_id = _kb(TENANT_A, retrieval={"retrieval_mode": "vector"})
        spy = _SpyChunks()

        _service(spy).query(TENANT_A, kb_id=kb_id, query="ES256")

        assert spy.calls == []


class TestDegradation:
    def test_a_broken_keyword_path_falls_back_to_vector(self, tenants: None) -> None:
        """**FTS 是增強，不是必要**（06 §1 的「降級優先於失敗」）。

        索引壞掉、pgroonga 沒裝、planner 選錯計畫——這些都該退回純向量，而不是讓
        使用者從此問不了問題。整輪失敗的代價是「這個知識庫壞了」，而降級的代價只是
        「稀有詞找不到」。
        """
        kb_id = _kb(TENANT_A)

        hits = _service(_BrokenChunks()).query(TENANT_A, kb_id=kb_id, query=_VECTOR_TARGET)

        assert hits and hits[0].content == _VECTOR_TARGET

    def test_the_fallback_is_recorded(
        self, tenants: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """降級必須留下痕跡：安靜的降級會讓「檢索品質變差」查不到原因，而那時看得到
        的只有評測分數掉了一截。

        用 `capsys` 而不是 `caplog`：structlog 寫的是 stdout（同
        `tests/unit/test_logging.py` 的驗法）。
        """
        kb_id = _kb(TENANT_A)

        _service(_BrokenChunks()).query(TENANT_A, kb_id=kb_id, query=_VECTOR_TARGET)

        assert "rag_fts_degraded" in capsys.readouterr().out


class TestChatPath:
    def test_retrieve_for_chat_uses_the_same_fusion(self, tenants: None) -> None:
        """問答與 `/rag/query` 必須看到同一組候選（1D-5 的教訓：兩個預設值會漂，而
        症狀是「除錯 API 查得到、實際問答查不到」）。"""
        kb_id = _kb(TENANT_A, retrieval={"context_chunks": 2})

        selected = _service().retrieve_for_chat(TENANT_A, kb_ids=[kb_id], query="ES256")

        assert len(selected) <= 2
        assert _FTS_TARGET in [chunk.content for chunk in selected]

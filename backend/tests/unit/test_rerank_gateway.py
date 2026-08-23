"""驗收：Gateway 的 rerank（06 §3.1／§4、11 §4、13 §4 工作包 2B-3）。

**rerank 是讀路徑上最脆弱的一環**（06 §6 明寫），而它的脆弱處不在準確度，在**它會壞**：
外部服務、GPU、模型載入，任何一項出事都不該讓使用者問不了問題。因此這一層的規則與
embedding／chat 那兩條**刻意不同**：

1. **不重試**。11 §4.1 的重試上限是給冪等且必要的呼叫用的；rerank 是可跳過的增強，而
   逾時上限只有 1.2s（11 §4 的預算）——重試一次就變 2.4s，使用者等的是那個，不是更好
   的排序。
2. **失敗往上拋，由 service 決定降級**（同 embedding）。Gateway 不吞例外：吞掉的話
   「rerank 從來沒成功過」與「rerank 正常」在上層看起來一模一樣。
3. **空清單不打 provider**。候選是空的時候沒有東西可排，而那一趟仍然要付延遲
   （TEI 要載入 batch、雲端 API 要一個 round-trip）。

**adapter 形狀不共用 `openai_compatible.py`**（13 §4 的 2B 開工前定案）：rerank 沒有
OpenAI 相容的共通形狀，TEI／Cohere／Jina／NVIDIA 各一套 request/response。這一層因此只
定義「我們要什麼」——一組 (原始索引, 分數)，由 adapter 各自翻譯。
"""

from __future__ import annotations

import pytest

from ai.gateway import AIGateway, RerankResult
from ai.gateway.providers import ProviderRerank, RerankedDocument, RerankProvider
from ai.gateway.providers.mock import MockRerankProvider
from core.exceptions import ProviderError, ProviderTimeoutError

_DOCS = ["員工請假應於三日前提出申請", "出差旅費以實報實銷", "年度考核於十二月進行"]


class _SpyProvider:
    """記下被呼叫了幾次、帶什麼參數。"""

    name = "spy"

    def __init__(self, result: ProviderRerank | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._result = result or ProviderRerank(
            results=[RerankedDocument(index=0, score=0.9)], model="spy-rerank"
        )

    def rerank(
        self, query: str, documents: list[str], *, model: str, timeout_seconds: float
    ) -> ProviderRerank:
        self.calls.append(
            {"query": query, "documents": documents, "model": model, "timeout": timeout_seconds}
        )
        return self._result


class _FailingProvider:
    name = "failing"

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def rerank(
        self, query: str, documents: list[str], *, model: str, timeout_seconds: float
    ) -> ProviderRerank:
        self.calls += 1
        raise self.error


def _gateway(provider: RerankProvider) -> AIGateway:
    return AIGateway(rerank_provider=provider, retry_backoff_seconds=())


class TestResult:
    def test_it_returns_documents_ordered_by_score(self) -> None:
        """**回的是名次，不是重排過的文字**：呼叫端手上有 `RetrievedChunk`（帶 chunk_id、
        頁碼、檔名），只拿文字回來的話那些欄位就對不回去了。"""
        provider = _SpyProvider(
            ProviderRerank(
                results=[
                    RerankedDocument(index=2, score=0.91),
                    RerankedDocument(index=0, score=0.42),
                ],
                model="spy-rerank",
            )
        )

        result = _gateway(provider).rerank("請假規定", _DOCS, model="m", top_n=2)

        assert isinstance(result, RerankResult)
        assert [(doc.index, doc.score) for doc in result.documents] == [(2, 0.91), (0, 0.42)]

    def test_it_records_the_model_the_provider_reported(self) -> None:
        """同 embedding：provider 會做別名解析，而「當時用了哪個模型」是 06 §1 的
        版本化貫穿要留的快照。"""
        result = _gateway(_SpyProvider()).rerank("問題", _DOCS, model="requested", top_n=1)

        assert result.model == "spy-rerank"
        assert result.provider == "spy"

    def test_an_empty_document_list_never_reaches_the_provider(self) -> None:
        """沒有候選就沒有東西可排，而那一趟仍然要付延遲（TEI 要載 batch、雲端要一次
        round-trip）。"""
        provider = _SpyProvider()

        result = _gateway(provider).rerank("問題", [], model="m", top_n=8)

        assert result.documents == []
        assert provider.calls == []

    def test_top_n_larger_than_the_candidate_count_is_fine(self) -> None:
        provider = _SpyProvider(
            ProviderRerank(results=[RerankedDocument(index=0, score=0.5)], model="m")
        )

        assert len(_gateway(provider).rerank("問題", _DOCS[:1], model="m", top_n=50).documents) == 1


class TestCallShape:
    def test_the_timeout_comes_from_the_gateway(self) -> None:
        """11 §4.1 的 timeout 字典是全域的；adapter 各自訂一個值會讓那份字典失去意義
        （同 embedding／chat 的理由）。"""
        provider = _SpyProvider()

        _gateway(provider).rerank("問題", _DOCS, model="m", top_n=3, timeout_seconds=1.2)

        assert provider.calls[0]["timeout"] == 1.2

    def test_the_query_and_documents_are_passed_through_untouched(self) -> None:
        """cross-encoder 讀的是原文：截斷、正規化、加提示詞都會改變它看到的東西，而
        那正是我們花這 800ms 想買的判斷。"""
        provider = _SpyProvider()

        _gateway(provider).rerank("請假規定", _DOCS, model="m", top_n=3)

        assert provider.calls[0]["query"] == "請假規定"
        assert provider.calls[0]["documents"] == _DOCS


class TestFailure:
    def test_it_does_not_retry(self) -> None:
        """**與 embedding 相反**（見模組 docstring 第 1 點）：逾時上限 1.2s，重試一次
        就變 2.4s，而使用者等的是那個，不是更好的排序。"""
        provider = _FailingProvider(ProviderTimeoutError("rerank 逾時"))

        with pytest.raises(ProviderError):
            _gateway(provider).rerank("問題", _DOCS, model="m", top_n=3)

        assert provider.calls == 1

    def test_a_retryable_error_is_still_not_retried(self) -> None:
        """429／5xx 也一樣：它們對「可跳過的增強」而言與其他錯誤沒有分別。"""
        # `retryable` 是**型別屬性**（core/exceptions.py：可否重試由型別決定），
        # `ProviderError` 本身即為可重試的那一類。
        assert ProviderError.retryable is True
        provider = _FailingProvider(ProviderError("上游忙碌"))

        with pytest.raises(ProviderError):
            _gateway(provider).rerank("問題", _DOCS, model="m", top_n=3)

        assert provider.calls == 1

    def test_failures_reach_the_caller(self) -> None:
        """Gateway 不吞：吞掉的話「rerank 從來沒成功過」與「rerank 正常」在上層看起來
        一模一樣，而降級是 service 的決定（`RetrievalService`）。"""
        with pytest.raises(ProviderError):
            _gateway(_FailingProvider(ProviderError("壞了"))).rerank(
                "問題", _DOCS, model="m", top_n=3
            )


class TestMockProvider:
    """CI 綠燈不得依賴 GPU（13 §4 的 2B 定案）——自動測試一律走這個。"""

    def test_it_is_deterministic(self) -> None:
        first = MockRerankProvider().rerank("請假", _DOCS, model="m", timeout_seconds=1.0)
        second = MockRerankProvider().rerank("請假", _DOCS, model="m", timeout_seconds=1.0)

        assert [d.score for d in first.results] == [d.score for d in second.results]

    def test_it_reacts_to_the_query(self) -> None:
        """**假 provider 也要有訊號**：分數與查詢無關的話，「rerank 有沒有真的改變順序」
        這件事在測試裡就驗不出來——而那是 2B-3 唯一要驗的行為。
        """
        result = MockRerankProvider().rerank(_DOCS[2], _DOCS, model="m", timeout_seconds=1.0)

        assert result.results[0].index == 2

    def test_scores_stay_inside_the_unit_interval(self) -> None:
        """06 §3.1 的絕對門檻 0.3 是 cross-encoder 的尺度（0~1）。假 provider 若回一個
        別的範圍，門檻的測試就會在假資料上通過、在真 provider 上失效。"""
        result = MockRerankProvider().rerank(
            "完全無關的問題", _DOCS, model="m", timeout_seconds=1.0
        )

        assert all(0.0 <= doc.score <= 1.0 for doc in result.results)

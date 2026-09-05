"""驗收：真的 rerank adapter —— 自架 TEI 與 Jina（06 §3.1／§3.4、11 §4、13 §4 工作包 2B-4）。

2B-3 立了規則（不重試、失敗往上拋、空清單不打 provider）而只有 `MockRerankProvider`；
這一包把規則接到真的 HTTP 上，並第一次讓 06 §3.1 的絕對門檻 0.3 有真的分數可以套。

**兩家不共用一個 adapter**（13 §4 的 2B 開工前定案）：rerank 沒有 OpenAI 相容的共通
形狀——TEI 的 `/rerank` 收 `texts`、回一個裸陣列且不回報模型名；Jina 收 `documents`、
回 `{"results": [{"index", "relevance_score"}], "model": ...}`。1C-5「五家共用一個
adapter」的紅利在這裡不成立，因此本檔驗的是「兩個翻譯 × 一組共通規則」。

**為什麼要有第二家**：`ai_rerank_provider` 是設定值，而「換得掉」這件事只有在真的有
第二家的時候才驗得到。Jina 同時是沒有 GPU 的機器唯一能用的選項。

**測試不打真 API**（CLAUDE.md 鐵則）：全部走 `httpx.MockTransport`。真 TEI 的連通性由
`make verify-provider CAPABILITY=rerank` 手動驗（守門見 `test_dev_launcher.py`）。

四件事錯了不會有錯誤訊息，只會讓排序安靜地變差或整條 rerank 靜靜地停掉：

1. **索引**。回來的是名次，而我們要的是「原始清單的第幾筆」。對錯位的話每一筆都合法、
   每一筆都指到別的 chunk——而引用會指向錯的文件，看起來完全正常。
2. **分數尺度**。TEI 的 `raw_scores=true` 回的是 logits（可為負、無上界），套 0.3 的
   絕對門檻不是「品質變好」，是隨機砍掉候選。
3. **超長輸入**。`bge-reranker-v2-m3` 的 context 是 512 token，而 chunk 上限是
   `chunk_target_tokens=512` 加標題與 overlap——不開 `truncate` 的話整批 422，
   症狀是「rerank 永遠處於降級狀態」，而降級是靜默的。
4. **金鑰外洩**。Jina 的金鑰在 header 裡，而 provider 物件會整個被丟進 log。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ai.gateway.providers import ProviderRerank, RerankProvider
from ai.gateway.providers.rerank import (
    JINA_BASE_URL,
    TEI_DEFAULT_BASE_URL,
    JinaRerankProvider,
    TeiRerankProvider,
)
from core.exceptions import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

_KEY = "jina_secret-do-not-leak-1234567890"
_TEI_MODEL = "BAAI/bge-reranker-v2-m3"
_JINA_MODEL = "jina-reranker-v2-base-multilingual"
_QUERY = "請假要提前幾天申請？"
_DOCS = [
    "年度考核於十二月進行",
    "員工請假應於三日前提出申請",
    "出差旅費以實報實銷",
]


def _tei(handler: Any, *, base_url: str | None = None) -> TeiRerankProvider:
    return TeiRerankProvider(base_url=base_url, transport=httpx.MockTransport(handler))


def _jina(handler: Any, *, api_key: str = _KEY) -> JinaRerankProvider:
    return JinaRerankProvider(api_key=api_key, transport=httpx.MockTransport(handler))


def _tei_ok(captured: list[httpx.Request], *, body: Any = None) -> Any:
    """TEI 的正常回應：**裸陣列**，已依分數由高到低排好。"""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        payload = body
        if payload is None:
            count = len(json.loads(request.content)["texts"])
            payload = [
                {"index": index, "score": round(0.9 - 0.1 * position, 4)}
                for position, index in enumerate(reversed(range(count)))
            ]
        return httpx.Response(200, json=payload)

    return handler


def _jina_ok(captured: list[httpx.Request], *, body: Any = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        payload = body
        if payload is None:
            count = len(json.loads(request.content)["documents"])
            payload = {
                "model": _JINA_MODEL,
                "usage": {"total_tokens": 123},
                "results": [
                    {"index": index, "relevance_score": round(0.9 - 0.1 * position, 4)}
                    for position, index in enumerate(reversed(range(count)))
                ],
            }
        return httpx.Response(200, json=payload)

    return handler


def _status(code: int, *, body: Any = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(code, json=body if body is not None else {"error": "boom"})

    return handler


class TestBothFamiliesSatisfyTheProtocol:
    """`RerankProvider` 是 Gateway 認得的唯一形狀（2B-3）。"""

    def test_they_are_rerank_providers(self) -> None:
        assert isinstance(_tei(_tei_ok([])), RerankProvider)
        assert isinstance(_jina(_jina_ok([])), RerankProvider)

    def test_each_reports_its_own_name(self) -> None:
        """`name` 會進 log 與 `usage.rag`——兩家都叫 "rerank" 的話，「哪一家在跑」
        就只能靠猜，而降級統計是按 provider 分的。"""
        assert _tei(_tei_ok([])).name == "tei"
        assert _jina(_jina_ok([])).name == "jina"

    @pytest.mark.parametrize("family", ["tei", "jina"])
    def test_an_empty_candidate_list_never_touches_the_network(self, family: str) -> None:
        """Gateway 已經擋了一層；adapter 再擋是因為它也會被直接使用
        （`make verify-provider`），而那一趟仍然要付延遲——TEI 要載 batch。"""
        calls: list[httpx.Request] = []
        provider: RerankProvider = (
            _tei(_tei_ok(calls)) if family == "tei" else _jina(_jina_ok(calls))
        )

        result = provider.rerank(_QUERY, [], model=_TEI_MODEL, timeout_seconds=1.2)

        assert result.results == []
        assert calls == []


class TestTeiRequestShape:
    """送出去的東西長什麼樣。TEI 的預設值全部是我們不要的，所以每一個都得明寫。"""

    def test_it_posts_to_the_rerank_endpoint(self) -> None:
        captured: list[httpx.Request] = []

        _tei(_tei_ok(captured), base_url="http://tei.internal:8080").rerank(
            _QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2
        )

        assert str(captured[0].url) == "http://tei.internal:8080/rerank"

    def test_it_defaults_to_the_local_container(self) -> None:
        """位址沒設時打本機的 TEI（同 vLLM 的 `127.0.0.1:8000`）：自架服務的預設
        部署就在本機，而設定漏了的後果是連線被拒——那是一個看得見的失敗。"""
        captured: list[httpx.Request] = []

        _tei(_tei_ok(captured)).rerank(_QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2)

        assert str(captured[0].url).startswith(TEI_DEFAULT_BASE_URL)

    def test_it_sends_the_query_and_the_texts_in_order(self) -> None:
        """**順序就是索引的定義**：回來的 `index` 指的是這個陣列的第幾筆。"""
        captured: list[httpx.Request] = []

        _tei(_tei_ok(captured)).rerank(_QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2)
        payload = json.loads(captured[0].content)

        assert payload["query"] == _QUERY
        assert payload["texts"] == _DOCS

    def test_it_asks_for_normalised_scores(self) -> None:
        """`raw_scores=true` 回的是 logits（可為負、無上界）。06 §3.1 的絕對門檻 0.3
        是 0~1 尺度上的數字——送錯這個旗標，門檻就從「品質關卡」變成隨機切割。"""
        captured: list[httpx.Request] = []

        _tei(_tei_ok(captured)).rerank(_QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2)

        assert json.loads(captured[0].content)["raw_scores"] is False

    def test_it_truncates_instead_of_failing_the_whole_batch(self) -> None:
        """`bge-reranker-v2-m3` 的 context 是 512 token，而一個 chunk 加上標題就可能
        超過。不開 truncate 的話，一段過長的候選會讓**整批** 413/422——而降級是靜默的，
        症狀只是「rerank 好像沒在動」。"""
        captured: list[httpx.Request] = []

        _tei(_tei_ok(captured)).rerank(_QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2)

        assert json.loads(captured[0].content)["truncate"] is True

    def test_it_does_not_ask_for_the_texts_back(self) -> None:
        """我們用索引對回自己的 `RetrievedChunk`，原文一個字都不需要。24 段候選回傳
        原文是每次查詢多幾十 KB，全落在使用者等待的 1.2s 預算裡。"""
        captured: list[httpx.Request] = []

        _tei(_tei_ok(captured)).rerank(_QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2)

        assert json.loads(captured[0].content)["return_text"] is False

    def test_the_timeout_reaches_the_http_layer(self) -> None:
        """11 §4：rerank 的預算是 1.2s，而那個數字由 Gateway 傳進來（11 §4.1 的
        timeout 字典是全域的）。沒傳到底層的話，掛住的 TEI 會把整個問答一起拖住。"""
        captured: list[httpx.Request] = []

        _tei(_tei_ok(captured)).rerank(_QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2)

        assert captured[0].extensions["timeout"]["read"] == 1.2


class TestTeiResponse:
    def test_it_returns_original_indices_with_scores(self) -> None:
        """**回名次不等於回索引**：`_DOCS[1]` 才是答案，而它在回應裡排第一。"""
        body = [
            {"index": 1, "score": 0.98},
            {"index": 2, "score": 0.21},
            {"index": 0, "score": 0.05},
        ]

        result = _tei(_tei_ok([], body=body)).rerank(
            _QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2
        )

        assert [(doc.index, doc.score) for doc in result.results] == [
            (1, 0.98),
            (2, 0.21),
            (0, 0.05),
        ]

    def test_it_sorts_by_score_even_if_the_service_does_not(self) -> None:
        """TEI 目前回的是排好的，但那是它的實作細節。Gateway 只做 `[:top_n]` 切片
        ——順序錯的話，切片就切掉了分數最高的那幾段。"""
        body = [
            {"index": 0, "score": 0.10},
            {"index": 2, "score": 0.90},
            {"index": 1, "score": 0.50},
        ]

        result = _tei(_tei_ok([], body=body)).rerank(
            _QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2
        )

        assert [doc.index for doc in result.results] == [2, 1, 0]

    def test_ties_break_on_the_original_index(self) -> None:
        """同分時的順序要決定（同 RRF 與 MockRerankProvider）：不決定的話，同一個問題
        兩次查詢可能給出不同的引用，而那種不穩定沒有人查得出原因。"""
        body = [
            {"index": 2, "score": 0.5},
            {"index": 0, "score": 0.5},
            {"index": 1, "score": 0.5},
        ]

        result = _tei(_tei_ok([], body=body)).rerank(
            _QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2
        )

        assert [doc.index for doc in result.results] == [0, 1, 2]

    def test_it_reports_the_configured_model(self) -> None:
        """TEI 一個容器只服務一個模型，回應裡也不帶模型名。06 §1 的版本化貫穿要留下
        「當時用了哪個模型」的快照，所以這裡照實回報我們請求的那個名字。"""
        result = _tei(_tei_ok([])).rerank(_QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2)

        assert result.model == _TEI_MODEL

    def test_scores_stay_inside_the_zero_to_one_scale(self) -> None:
        """絕對門檻 0.3 靠這個尺度。adapter 不做任何縮放——真的收到 1.5 或 -3 的話，
        那是 `raw_scores` 送錯了，而安靜地 clamp 會把那個 bug 藏起來。"""
        result = _tei(_tei_ok([])).rerank(_QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2)

        assert all(0.0 <= doc.score <= 1.0 for doc in result.results)


class TestJina:
    """第二家：形狀完全不同，規則完全相同。"""

    def test_it_posts_to_the_public_endpoint_with_the_key(self) -> None:
        captured: list[httpx.Request] = []

        _jina(_jina_ok(captured)).rerank(_QUERY, _DOCS, model=_JINA_MODEL, timeout_seconds=1.2)

        assert str(captured[0].url) == f"{JINA_BASE_URL}/rerank"
        assert captured[0].headers["Authorization"] == f"Bearer {_KEY}"

    def test_it_speaks_jinas_field_names(self) -> None:
        """`documents` 不是 `texts`，而 `model` 是必填——TEI 的欄位名送過去會被退 422，
        而那是「換一家就整條 rerank 停掉」。"""
        captured: list[httpx.Request] = []

        _jina(_jina_ok(captured)).rerank(_QUERY, _DOCS, model=_JINA_MODEL, timeout_seconds=1.2)
        payload = json.loads(captured[0].content)

        assert payload["model"] == _JINA_MODEL
        assert payload["query"] == _QUERY
        assert payload["documents"] == _DOCS
        assert payload["return_documents"] is False

    def test_it_reads_relevance_scores(self) -> None:
        body = {
            "model": _JINA_MODEL,
            "results": [
                {"index": 1, "relevance_score": 0.97},
                {"index": 0, "relevance_score": 0.12},
            ],
        }

        result = _jina(_jina_ok([], body=body)).rerank(
            _QUERY, _DOCS, model=_JINA_MODEL, timeout_seconds=1.2
        )

        assert [(doc.index, doc.score) for doc in result.results] == [(1, 0.97), (0, 0.12)]

    def test_it_reports_the_model_the_service_actually_used(self) -> None:
        """雲端 API 會做別名解析（同 embedding 的 1C-5）。回報的是真的被用到的那個。"""
        body = {
            "model": "jina-reranker-v2-base-multilingual-2024",
            "results": [{"index": 0, "relevance_score": 0.5}],
        }

        result = _jina(_jina_ok([], body=body)).rerank(
            _QUERY, _DOCS, model=_JINA_MODEL, timeout_seconds=1.2
        )

        assert result.model == "jina-reranker-v2-base-multilingual-2024"

    def test_the_key_never_reaches_a_repr(self) -> None:
        """provider 物件被整個丟進 log 是常態——設定 dump、例外的 locals、除錯的 print。"""
        assert _KEY not in repr(_jina(_jina_ok([])))

    def test_the_key_never_reaches_an_error_message(self) -> None:
        """錯誤訊息會經 `usage.rag` 與 log 落地。第三方的原文常把整個請求回貼回來，
        所以訊息一律由我們自己組（鐵則 9）。"""
        leaky = {"detail": f"invalid key {_KEY}"}

        with pytest.raises(ProviderError) as caught:
            _jina(_status(401, body=leaky)).rerank(
                _QUERY, _DOCS, model=_JINA_MODEL, timeout_seconds=1.2
            )

        assert _KEY not in str(caught.value)
        assert _KEY not in json.dumps(caught.value.details or {})


class TestErrorTranslation:
    """狀態碼 → 我們的例外型別。

    **Gateway 不重試 rerank**（2B-3），所以 `retryable` 在這條路上不決定行為——它決定
    的是分類與統計：「TEI 因為過載被跳過」與「金鑰過期所以永遠跳過」是兩件事，而混在
    一起的話，rerank 靜靜地停了三天不會有人看得出來。
    """

    @pytest.mark.parametrize("family", ["tei", "jina"])
    def test_a_timeout_is_a_timeout(self, family: str) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        provider: RerankProvider = _tei(handler) if family == "tei" else _jina(handler)

        with pytest.raises(ProviderTimeoutError):
            provider.rerank(_QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2)

    @pytest.mark.parametrize("family", ["tei", "jina"])
    def test_a_connection_failure_is_unavailable_not_a_crash(self, family: str) -> None:
        """TEI 沒開是開發機上最常見的一種——它必須降級成「跳過 rerank」，而不是讓
        問答失敗。裸的 httpx 例外會冒到 service 的 `except ProviderError` 之外。"""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        provider: RerankProvider = _tei(handler) if family == "tei" else _jina(handler)

        with pytest.raises(ProviderUnavailableError):
            provider.rerank(_QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2)

    @pytest.mark.parametrize("family", ["tei", "jina"])
    def test_rate_limiting_is_retryable(self, family: str) -> None:
        provider: RerankProvider = _tei(_status(429)) if family == "tei" else _jina(_status(429))

        with pytest.raises(ProviderRateLimitedError) as caught:
            provider.rerank(_QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2)

        assert caught.value.retryable is True

    def test_rejected_credentials_are_not_retryable(self) -> None:
        with pytest.raises(ProviderAuthError) as caught:
            _jina(_status(403)).rerank(_QUERY, _DOCS, model=_JINA_MODEL, timeout_seconds=1.2)

        assert caught.value.retryable is False

    @pytest.mark.parametrize("status", [413, 422])
    def test_oversized_input_is_not_retryable(self, status: int) -> None:
        """413（batch 太大）與 422（tokenize 失敗）是**我們送出去的東西**的問題，
        重試三次還是一樣。它們該指向 `rag_hybrid_candidates` 或 chunk 大小，不是
        「TEI 不穩」——而那個判斷靠的就是這個旗標。"""
        with pytest.raises(ProviderError) as caught:
            _tei(_status(status)).rerank(_QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2)

        assert caught.value.retryable is False

    @pytest.mark.parametrize("status", [424, 500, 503])
    def test_service_side_failures_are_retryable(self, status: int) -> None:
        """424 是 TEI 的「推論失敗」（GPU OOM 最常見）。"""
        with pytest.raises(ProviderError) as caught:
            _tei(_status(status)).rerank(_QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2)

        assert caught.value.retryable is True


class TestMalformedResponses:
    """200 但形狀不對：proxy 插了一頁 HTML、免費額度用完回了別的東西、版本升級改了欄位。

    這些一律要變成 `ProviderError`——裸的 `KeyError` / `IndexError` 會穿過 service 的
    降級處理（它只接 `ProviderError`），把一次可跳過的增強變成一次失敗的問答。
    """

    def test_tei_rejects_a_body_that_is_not_a_list(self) -> None:
        with pytest.raises(ProviderError):
            _tei(_tei_ok([], body={"results": []})).rerank(
                _QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2
            )

    def test_jina_rejects_a_body_without_results(self) -> None:
        with pytest.raises(ProviderError):
            _jina(_jina_ok([], body={"model": _JINA_MODEL})).rerank(
                _QUERY, _DOCS, model=_JINA_MODEL, timeout_seconds=1.2
            )

    def test_non_json_is_a_provider_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>502 Bad Gateway</html>")

        with pytest.raises(ProviderError):
            _tei(handler).rerank(_QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2)

    def test_an_index_outside_the_batch_is_refused(self) -> None:
        """**這一條是正確性而不是防禦**：索引 7 會被呼叫端拿去對 `RetrievedChunk`，
        而那份清單只有 3 筆。放行的話不是 `IndexError` 就是指到別的 chunk——引用會
        指向一份與答案無關的文件，看起來完全正常。"""
        body = [{"index": 7, "score": 0.9}]

        with pytest.raises(ProviderError):
            _tei(_tei_ok([], body=body)).rerank(
                _QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2
            )

    def test_a_missing_score_is_refused(self) -> None:
        body = [{"index": 0}]

        with pytest.raises(ProviderError):
            _tei(_tei_ok([], body=body)).rerank(
                _QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2
            )

    def test_a_duplicate_index_is_refused(self) -> None:
        """同一筆候選出現兩次 → 上層會把同一段 chunk 放進 context 兩遍，而 token 預算
        與引用編號都會跟著錯一位。"""
        body = [{"index": 0, "score": 0.9}, {"index": 0, "score": 0.4}]

        with pytest.raises(ProviderError):
            _tei(_tei_ok([], body=body)).rerank(
                _QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2
            )

    def test_returning_fewer_rows_than_sent_is_fine(self) -> None:
        """**少回是合法的**（有些家會自己截斷），少的那幾筆就是沒進 top_n。多回或亂回
        才是錯——那表示我們與服務對不上同一份候選。"""
        body = [{"index": 1, "score": 0.9}]

        result = _tei(_tei_ok([], body=body)).rerank(
            _QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2
        )

        assert [doc.index for doc in result.results] == [1]

    def test_the_result_is_the_gateways_own_type(self) -> None:
        result = _tei(_tei_ok([])).rerank(_QUERY, _DOCS, model=_TEI_MODEL, timeout_seconds=1.2)

        assert isinstance(result, ProviderRerank)


class TestSettingsWiring:
    """`AI_RERANK_PROVIDER` → 真 adapter。`build_gateway()` 是那個對照的唯一位置。"""

    @staticmethod
    def _rebuild(monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
        from ai.gateway import build_gateway
        from config.settings.app_settings import get_app_settings

        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_app_settings.cache_clear()
        return build_gateway()

    @pytest.fixture(autouse=True)
    def _reset_settings_cache(self) -> Any:
        from config.settings.app_settings import get_app_settings

        yield
        get_app_settings.cache_clear()

    def test_tei_builds_without_a_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """自架服務沒有金鑰概念——硬性要求會讓主線那條路走不通。"""
        gateway = self._rebuild(
            monkeypatch,
            AI_RERANK_PROVIDER="tei",
            AI_RERANK_API_KEY="",
            AI_RERANK_MODEL=_TEI_MODEL,
        )

        assert gateway.rerank_provider_name == "tei"

    def test_jina_without_a_key_fails_at_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail Fast（同 1C-1／1D-3a）：缺金鑰要在服務起來的當下就炸，而不是等第一個
        使用者提問——那時它只會變成一次靜默的降級，看起來像「rerank 沒什麼效果」。"""
        with pytest.raises(ProviderUnavailableError):
            self._rebuild(monkeypatch, AI_RERANK_PROVIDER="jina", AI_RERANK_API_KEY="")

    def test_jina_builds_with_a_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gateway = self._rebuild(
            monkeypatch,
            AI_RERANK_PROVIDER="jina",
            AI_RERANK_API_KEY=_KEY,
            AI_RERANK_MODEL=_JINA_MODEL,
        )

        assert gateway.rerank_provider_name == "jina"

    def test_the_default_is_still_mock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """漏設環境變數時要得到的是假分數，而不是一筆真帳單或一個起不來的服務
        （13 §4 的定案）。2B-4 接上真 adapter **不改**這個預設。

        **`_env_file=None` 才問得到「預設」**（同 `test_infra_config` 的用法）：
        `AppSettings` 的 `env_file` 直接讀 repo 根的 `.env`，而跑過第三次評測的開發機
        那裡就寫著 `AI_RERANK_PROVIDER=tei`——只 `delenv` 的話，這條測試會在「有人真的
        接上 TEI」的那一台變紅，而它要守的預設其實一個字都沒動。
        """
        from config.settings.app_settings import AppSettings

        monkeypatch.delenv("AI_RERANK_PROVIDER", raising=False)

        assert AppSettings(_env_file=None).ai_rerank_provider == "mock"  # type: ignore[call-arg]

    def test_every_configurable_provider_is_buildable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """設定的 Literal 與工廠是分開維護的，而漏掉的那一項只有在有人真的把設定改成
        它的時候才會走到——那通常是在正式環境。"""
        from typing import get_args

        from config.settings.app_settings import AppSettings

        configurable = get_args(AppSettings.model_fields["ai_rerank_provider"].annotation)

        for name in configurable:
            gateway = self._rebuild(monkeypatch, AI_RERANK_PROVIDER=name, AI_RERANK_API_KEY=_KEY)
            assert gateway.rerank_provider_name == name

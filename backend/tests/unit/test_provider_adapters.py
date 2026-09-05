"""驗收：真 provider 的 embedding adapter（06 §4、02 §2、13 §3 工作包 1C-5）。

1C-1 立了規則（timeout、重試、計量）而只有 MockProvider；這一包把規則接到真的 HTTP 上。

**五家共用一個 adapter**：Gemini、OpenAI、OpenRouter、NVIDIA NIM、vLLM 全部提供
OpenAI 相容的 `/v1/embeddings`，差別只有 base URL、金鑰與支不支援 `dimensions`。寫五份
的話，五份會各自漂——而漏改的那一份只在切換到那家時才會走到，也就是沒有人測的時候。
因此本檔驗的是「一個實作 × 一張廠商表」，而不是五個實作。

**不裝任何廠商 SDK**：五家都是同一種 REST，直接用已有的 httpx。裝五個 SDK 是五個相依、
五組版本風險，換來的只是同一個 POST。

**測試不打真 API**（CLAUDE.md）：全部走 `httpx.MockTransport`，驗的是「我們送出去的請求
長什麼樣、回來的東西怎麼解讀、出錯時怎麼分類」。真 API 的連通性由 `make verify-provider`
手動驗（見 `tests/unit/test_dev_launcher.py::TestProviderVerification`）。

四件事錯了不會有錯誤訊息，只會讓檢索安靜地變差或帳單變貴：

1. **向量順序**。回應的順序不保證等於送出的順序（所以 OpenAI 的回應才有 `index` 欄位）。
   照收的話，`EmbeddingService` 的 `zip(batch, vectors)` 會把 A 的向量配給 B 的 chunk
   ——每一筆都合法、每一筆都錯，而檢索結果只是「怪」。
2. **維度**。Gemini 預設回 3072，不送 `dimensions` 就塞不進 `halfvec(1536)`。
3. **可否重試的分類**。API key 打錯是 401，重試三次還是 401——只是把六分鐘的延遲加在
   一個確定的結論上；而 429 不重試等於把一次尖峰變成一份失敗的文件。
4. **金鑰不外洩**。它會出現在 header、URL 與第三方的錯誤訊息裡，而 `document.error`
   會回到租戶手上。
"""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest

from ai.gateway.providers.openai_compatible import (
    VENDORS,
    OpenAICompatibleProvider,
    VendorSpec,
)
from core.exceptions import (
    ModelNotEnabledError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

_KEY = "sk-secret-do-not-leak-1234567890"
_MODEL = "gemini-embedding-2"


def _one_hot(index: int, dimensions: int) -> list[float]:
    """第 `index` 格是 1、其餘是 0。

    **用方向而不是長度當標記**：adapter 會把向量正規化成單位長度，所以「第 i 筆的每一格
    都是 i」這種標記會被抹平（全部變成同一個向量）。one-hot 本身就是單位長度，正規化
    對它是恆等變換，順序因此驗得出來。
    """
    vector = [0.0] * dimensions
    vector[index] = 1.0
    return vector


def _embedding_response(count: int, *, dimensions: int = 8, model: str = _MODEL) -> dict[str, Any]:
    """OpenAI 格式的回應。`index` 刻意逆序——順序由它決定，不由陣列位置決定。"""
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": _one_hot(index, dimensions)}
            for index in reversed(range(count))
        ],
        "model": model,
        "usage": {"prompt_tokens": 42, "total_tokens": 42},
    }


def _provider(
    handler: Any, *, vendor: str = "gemini", dimensions: int | None = 4
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        vendor=vendor,
        api_key=_KEY,
        dimensions=dimensions,
        transport=httpx.MockTransport(handler),
    )


def _ok(captured: list[httpx.Request], **kwargs: Any) -> Any:
    """正常回應。**筆數由請求決定**——真的 provider 也是這樣，而寫死一個數字會讓
    「回傳筆數與送出筆數不符」那條防線在其他測試裡誤觸。"""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        count = len(json.loads(request.content)["input"])
        return httpx.Response(200, json=_embedding_response(count, **kwargs))

    return handler


class TestVendorRegistry:
    """廠商表：加一家 = 加一列設定，不是加一個實作。"""

    def test_every_configurable_provider_has_a_spec(self) -> None:
        """設定裡選得到的 provider，表裡就必須有——否則選了它會在執行期才炸。

        兩邊是分開維護的（`AppSettings` 的 Literal 與這張表），而漏掉的那一項只有在
        有人真的把設定改成它的時候才會走到，那通常是在正式環境。
        """
        from typing import get_args

        from config.settings.app_settings import AppSettings

        configurable = set(get_args(AppSettings.model_fields["ai_embedding_provider"].annotation))

        assert configurable - {"mock"} == set(VENDORS)

    def test_the_six_vendors_are_present(self) -> None:
        assert set(VENDORS) == {"gemini", "openai", "openrouter", "nvidia", "vllm", "tei"}

    @pytest.mark.parametrize("vendor", sorted(VENDORS))
    def test_no_credentials_are_baked_into_the_table(self, vendor: str) -> None:
        """表裡只有位址，沒有金鑰（鐵則 9）。

        寫死一把 key 的後果不只是外洩——它會讓「忘了設環境變數」的部署照樣跑得起來，
        而帳單記在別人頭上。
        """
        spec: VendorSpec = VENDORS[vendor]

        blob = json.dumps(
            {"base_url": spec.base_url, "vendor": vendor, "requires_key": spec.requires_api_key}
        ).lower()
        for leak in ("sk-", "bearer ", "api-key:", "aiza"):
            assert leak not in blob, f"{vendor} 的設定裡疑似夾了憑證：{leak}"

    @pytest.mark.parametrize("vendor", sorted(VENDORS))
    def test_remote_vendors_use_tls(self, vendor: str) -> None:
        """本機的兩家（自架 vLLM、自架 TEI）以外，一律 https——金鑰會跟著每一次請求送出去。"""
        spec = VENDORS[vendor]

        if vendor in {"vllm", "tei"}:
            assert spec.requires_api_key is False, f"本機的 {vendor} 不該要求金鑰"
        else:
            assert spec.base_url.startswith("https://"), f"{vendor} 不是 https"
            assert spec.requires_api_key is True


class TestRequestShape:
    def test_it_posts_to_the_embeddings_path(self) -> None:
        captured: list[httpx.Request] = []

        _provider(_ok(captured)).embed(["a", "b"], model=_MODEL, timeout_seconds=5.0)

        assert captured[0].method == "POST"
        assert str(captured[0].url).startswith(VENDORS["gemini"].base_url)
        assert str(captured[0].url).endswith("/embeddings")

    def test_the_batch_and_model_are_sent(self) -> None:
        captured: list[httpx.Request] = []

        _provider(_ok(captured)).embed(["第一段", "第二段"], model=_MODEL, timeout_seconds=5.0)

        body = json.loads(captured[0].content)
        assert body["input"] == ["第一段", "第二段"]
        assert body["model"] == _MODEL

    def test_the_dimension_is_requested_explicitly(self) -> None:
        """**不送 `dimensions` 的話 Gemini 回 3072**，而欄位是 `halfvec(1536)`。

        症狀是每一次寫入都被 DB 以「expected 1536 dimensions」擋下，而錯誤指向 INSERT
        ——看不出真正的原因在幾層之外的一個沒送出去的參數。
        """
        captured: list[httpx.Request] = []

        _provider(_ok(captured), dimensions=1536).embed(["a"], model=_MODEL, timeout_seconds=5.0)

        assert json.loads(captured[0].content)["dimensions"] == 1536

    def test_vendors_without_dimension_support_do_not_send_it(self) -> None:
        """NVIDIA 與 vLLM 的模型維度固定，送了會被退整批（400）。

        「支不支援」是廠商的性質，屬於那張表——寫在呼叫端的話，每個呼叫端都要記得
        判斷一次，而漏掉的那個只在切到那家時才會壞。
        """
        captured: list[httpx.Request] = []

        _provider(_ok(captured), vendor="vllm", dimensions=1536).embed(
            ["a"], model="bge-m3", timeout_seconds=5.0
        )

        assert "dimensions" not in json.loads(captured[0].content)

    def test_the_api_key_travels_as_a_bearer_token(self) -> None:
        captured: list[httpx.Request] = []

        _provider(_ok(captured)).embed(["a"], model=_MODEL, timeout_seconds=5.0)

        assert captured[0].headers["authorization"] == f"Bearer {_KEY}"

    def test_the_timeout_reaches_the_http_layer(self) -> None:
        """11 §4.1：每一次對外呼叫都要有 timeout，而且是 Gateway 傳進來的那個值。

        adapter 自己訂一個的話，11 §4.1 的全域字典就失去意義；沒有的話，provider 慢掉
        時 worker 會一個一個卡住，而症狀是「ETL 變慢」——看不出是外部依賴的問題。
        """
        captured: list[httpx.Request] = []

        _provider(_ok(captured)).embed(["a"], model=_MODEL, timeout_seconds=7.5)

        assert captured[0].extensions["timeout"]["read"] == 7.5


class TestResponseParsing:
    def test_vectors_come_back_in_input_order(self) -> None:
        """**回應的順序不保證等於送出的順序**——所以回應裡才有 `index`。

        照陣列位置收的話，`EmbeddingService` 的 `zip(batch, vectors)` 會把第一段的向量
        配給第三段的 chunk。每一筆都是合法的向量、每一筆都掛在錯的內容上，而檢索結果
        只是「怪」——沒有任何錯誤訊息，也沒有任何辦法事後發現。
        """
        captured: list[httpx.Request] = []

        result = _provider(_ok(captured)).embed(["a", "b", "c"], model=_MODEL, timeout_seconds=5.0)

        # index=i 的向量是「第 i 格為 1」，而回應的**陣列本身是逆序的**。照陣列位置
        # 收的話這裡會得到 [2, 1, 0]。
        assert [vector.index(1.0) for vector in result.vectors] == [0, 1, 2]

    def test_a_response_without_index_falls_back_to_array_order(self) -> None:
        """**Gemini 的相容端點不回 `index`**（2026-08-16 實測）。

        它的列只有 `object` 與 `embedding`。整批都沒有 index 時，唯一能做的假設就是
        「陣列順序 = 送出順序」——那也是所有 OpenAI 相容 client 的實際行為。

        這條測試是照著真實回應寫的：1C-5 第一次跑 `make verify-provider` 就是死在
        這裡（`回應缺少必要欄位`），而假的 HTTP 層當時全綠——因為假回應是照規格寫的，
        而 Gemini 沒有完全照規格。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"object": "embedding", "embedding": _one_hot(0, 8)},
                        {"object": "embedding", "embedding": _one_hot(1, 8)},
                    ],
                    "model": _MODEL,
                },
            )

        result = _provider(handler).embed(["a", "b"], model=_MODEL, timeout_seconds=5.0)

        assert [vector.index(1.0) for vector in result.vectors] == [0, 1]

    def test_a_missing_index_means_zero(self) -> None:
        """**`index` 為 0 時可能整個不出現**（2026-08-16 實測 Gemini）。

        那是 protobuf 省略預設值的慣例。嚴格要求每一列都有 `index` 會讓 Gemini 完全
        用不了——1C-5 第一次實測連撞兩次，就是這一條。

        這裡把回應做成**逆序且第 0 列省略 index**，同時驗兩件事：缺席當成 0、
        以及排序真的照 index 而不是陣列位置。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": 2, "embedding": _one_hot(2, 8)},
                        {"object": "embedding", "index": 1, "embedding": _one_hot(1, 8)},
                        {"object": "embedding", "embedding": _one_hot(0, 8)},  # index 0 省略
                    ],
                    "model": _MODEL,
                },
            )

        result = _provider(handler).embed(["a", "b", "c"], model=_MODEL, timeout_seconds=5.0)

        assert [vector.index(1.0) for vector in result.vectors] == [0, 1, 2]

    def test_indices_that_cannot_determine_an_order_are_rejected(self) -> None:
        """解出來的 index 不是 `0..n-1` 的排列 → **錯誤，不猜**。

        重複、跳號、超出範圍都落在這裡。這時猜任何一種順序都可能把向量配到錯的
        chunk 上，而那正是排序這件事存在的唯一理由。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": 5, "embedding": _one_hot(1, 8)},
                        {"object": "embedding", "index": 5, "embedding": _one_hot(0, 8)},
                    ],
                    "model": _MODEL,
                },
            )

        with pytest.raises(ProviderError):
            _provider(handler).embed(["a", "b"], model=_MODEL, timeout_seconds=5.0)

    def test_the_reported_model_comes_from_the_response(self) -> None:
        """別名解析（`text-embedding-3-small` → 帶日期的實際版本）之後，
        `UNIQUE(chunk, model, embedding_version)` 要記的是**真的被用到的那一個**。"""
        captured: list[httpx.Request] = []

        result = _provider(_ok(captured, model="gemini-embedding-2-2026-03")).embed(
            ["a"], model=_MODEL, timeout_seconds=5.0
        )

        assert result.model == "gemini-embedding-2-2026-03"

    def test_usage_is_taken_from_the_response(self) -> None:
        captured: list[httpx.Request] = []

        result = _provider(_ok(captured)).embed(["a", "b"], model=_MODEL, timeout_seconds=5.0)

        assert result.prompt_tokens == 42

    def test_a_missing_usage_block_is_estimated_not_zero(self) -> None:
        """provider 沒回報用量時要估一個非 0 的值（1C-1 的 Protocol 已載明）。

        填 0 會讓 2A 的成本統計把這次呼叫當成免費，而那種低估不會有人回報。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            body = _embedding_response(1)
            del body["usage"]
            return httpx.Response(200, json=body)

        result = _provider(handler).embed(["一段夠長的文字"], model=_MODEL, timeout_seconds=5.0)

        assert result.prompt_tokens > 0

    def test_vectors_are_unit_length(self) -> None:
        """截斷過的向量要正規化。

        `gemini-embedding-001` 的 Matryoshka 截斷**不會**自動正規化（`gemini-embedding-2`
        會）。cosine 距離本身對長度不敏感，所以這件事不會讓結果錯——但它會讓 MockProvider
        與真 provider 產出的向量性質不同，而 05 §5.3 若哪天改用內積 ops，排序就會變。
        統一在 adapter 收斂，呼叫端不必知道是哪一家。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            body = _embedding_response(1)
            body["data"][0]["embedding"] = [3.0, 4.0, 0.0, 0.0]  # 長度 5
            return httpx.Response(200, json=body)

        vector = _provider(handler).embed(["a"], model=_MODEL, timeout_seconds=5.0).vectors[0]

        assert abs(sum(value * value for value in vector) - 1.0) < 1e-6


class TestErrorMapping:
    """**可否重試由型別決定**（1C-1 定案）——這張表是那個判斷的唯一來源。

    判錯的兩種代價不對稱：把 401 當成可重試，是在一個確定的結論上加六分鐘延遲；
    把 429 當成不可重試，是把一次流量尖峰變成一份永久失敗的文件。
    """

    @pytest.mark.parametrize(
        ("status", "expected", "retryable"),
        [
            (429, ProviderRateLimitedError, True),
            (500, ProviderUnavailableError, True),
            (502, ProviderUnavailableError, True),
            (503, ProviderUnavailableError, True),
            (401, ProviderError, False),
            (403, ProviderError, False),
        ],
    )
    def test_http_status_maps_to_the_right_class(
        self, status: int, expected: type[ProviderError], retryable: bool
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"error": {"message": "boom"}})

        with pytest.raises(expected) as caught:
            _provider(handler).embed(["a"], model=_MODEL, timeout_seconds=5.0)

        assert caught.value.retryable is retryable

    def test_an_unknown_model_is_not_retryable(self) -> None:
        """模型名稱打錯、或那家沒有這個模型——重試幾次都一樣。

        這是 1C-5 之後最可能發生的設定錯誤：KB 的 `embedding_model` 是 per-KB 的，而
        provider 是全域的，兩者對不上時就會走到這裡。要立刻浮上來，而不是退避三次。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": {"message": "model not found"}})

        with pytest.raises(ModelNotEnabledError) as caught:
            _provider(handler).embed(["a"], model="nope", timeout_seconds=5.0)

        assert caught.value.retryable is False

    def test_the_error_names_the_model_not_the_url_path(self) -> None:
        """**404 說的必須是模型名。**

        `ModelNotEnabledError` 的訊息模板是「模型未啟用：{model}」，而原本填進去的是
        `response.request.url.path`——於是使用者與維運看到的是「模型未啟用：/embeddings」。
        那句話會被寫進 `document.error` 持久化，也是這條路徑上最需要說清楚的一刻：
        看到端點路徑的人會去查端點，而問題在 KB 的設定裡。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": {"message": "model not found"}})

        with pytest.raises(ModelNotEnabledError) as caught:
            _provider(handler).embed(["a"], model="typo-embedding", timeout_seconds=5.0)

        assert "typo-embedding" in str(caught.value)
        assert caught.value.details["model"] == "typo-embedding"
        # vendor 與狀態碼一併留著：分類與統計要用，而它們不洩漏 provider 的原文。
        assert caught.value.details["status"] == 404
        assert caught.value.details["vendor"] == "gemini"  # _provider 的預設廠商

    def test_a_rejected_parameter_also_names_the_model(self) -> None:
        """400 走同一條（固定維度的模型送了 dimensions、context 超過上限）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": {"message": "bad request"}})

        with pytest.raises(ModelNotEnabledError) as caught:
            _provider(handler).embed(["a"], model="fixed-dim-model", timeout_seconds=5.0)

        assert "fixed-dim-model" in str(caught.value)

    def test_a_network_timeout_becomes_a_provider_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        with pytest.raises(ProviderTimeoutError) as caught:
            _provider(handler).embed(["a"], model=_MODEL, timeout_seconds=5.0)

        assert caught.value.retryable is True

    def test_a_connection_error_is_retryable(self) -> None:
        """本機容器沒起、網路瞬斷——下一次通常就好了。"""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        with pytest.raises(ProviderUnavailableError) as caught:
            _provider(handler).embed(["a"], model=_MODEL, timeout_seconds=5.0)

        assert caught.value.retryable is True

    def test_a_malformed_response_does_not_crash_with_a_key_error(self) -> None:
        """回 200 但形狀不對（proxy 插了一頁 HTML、免費額度用完回了別的東西）。

        裸的 `KeyError` 會冒到 worker 的頂層並被記成一個看不出所以然的例外；收斂成
        `ProviderError` 之後，`document.error` 才說得出「是 provider 的問題」。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unexpected": "shape"})

        with pytest.raises(ProviderError):
            _provider(handler).embed(["a"], model=_MODEL, timeout_seconds=5.0)


class TestSecretHygiene:
    """金鑰會經 `document.error` 回到租戶手上（1B 結案 review 抓過同一類問題）。"""

    @pytest.mark.parametrize("status", [401, 429, 500])
    def test_the_api_key_never_appears_in_the_error(self, status: int) -> None:
        """第三方的錯誤訊息常把整個請求（含 header 或 URL）回貼給你。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status,
                json={"error": {"message": f"invalid credentials: {_KEY} for {request.url}"}},
            )

        with pytest.raises(ProviderError) as caught:
            _provider(handler).embed(["a"], model=_MODEL, timeout_seconds=5.0)

        assert _KEY not in str(caught.value)
        assert _KEY not in json.dumps(caught.value.details)

    def test_the_key_is_not_in_the_repr_of_the_provider(self) -> None:
        """provider 物件可能整個被丟進 log（設定 dump、例外的 locals）。"""
        provider = _provider(_ok([]))

        assert _KEY not in repr(provider)


class TestProtocolCompliance:
    def test_it_satisfies_the_embedding_provider_protocol(self) -> None:
        """形狀對不上時，Gateway 會在執行期才發現——而那是在 worker 裡。"""
        from ai.gateway.providers import EmbeddingProvider

        assert isinstance(_provider(_ok([])), EmbeddingProvider)

    def test_the_name_identifies_the_vendor(self) -> None:
        """`name` 會進 log 與 `EmbedResult.provider`——查「哪一家在出問題」靠它。"""
        assert _provider(_ok([]), vendor="openrouter").name == "openrouter"

    def test_an_empty_batch_never_reaches_the_network(self) -> None:
        """Gateway 已經擋了一層，adapter 自己也不該打——每一次呼叫都是錢與延遲。"""
        captured: list[httpx.Request] = []

        result = _provider(_ok(captured)).embed([], model=_MODEL, timeout_seconds=5.0)

        assert result.vectors == []
        assert captured == []


class TestConnectionReuse:
    """**每次呼叫新建一個 client = 每一批都重付一次 TCP + TLS 握手。**

    一份 500 頁的 PDF 是幾十個批次，對遠端端點（Gemini／OpenAI）每批多 100–300ms，
    全部落在使用者等待的處理時間上。純效能，不影響正確性——所以斷言的是「client 活著
    而且被重用」，那是連線池能發揮作用的前提。
    """

    def test_the_client_survives_a_call(self) -> None:
        """呼叫結束後 client 不關：關掉的話，連線池每批都要重建，重用等於沒有。"""
        from ai.gateway.providers.openai_compatible import _shared_client

        captured: list[httpx.Request] = []
        transport = httpx.MockTransport(_ok(captured))
        provider = OpenAICompatibleProvider(vendor="gemini", api_key=_KEY, transport=transport)

        provider.embed(["a"], model=_MODEL, timeout_seconds=5.0)

        client = _shared_client(provider._base_url, transport)
        assert client.is_closed is False

    def test_two_calls_share_one_client(self) -> None:
        from ai.gateway.providers.openai_compatible import _shared_client

        captured: list[httpx.Request] = []
        transport = httpx.MockTransport(_ok(captured))
        provider = OpenAICompatibleProvider(vendor="gemini", api_key=_KEY, transport=transport)
        base_url = provider._base_url

        provider.embed(["a"], model=_MODEL, timeout_seconds=5.0)
        first = _shared_client(base_url, transport)
        provider.embed(["b"], model=_MODEL, timeout_seconds=5.0)

        assert _shared_client(base_url, transport) is first

    def test_a_different_transport_gets_its_own_client(self) -> None:
        """**測試隔離的保證**：快取的鍵含 transport，否則上一條測試的假回應會服務
        下一條測試——而那種汙染看起來像「某些測試單獨跑才會過」。"""
        from ai.gateway.providers.openai_compatible import _shared_client

        captured: list[httpx.Request] = []
        one = httpx.MockTransport(_ok(captured))
        two = httpx.MockTransport(_ok(captured))
        base_url = OpenAICompatibleProvider(vendor="gemini", api_key=_KEY)._base_url

        assert _shared_client(base_url, one) is not _shared_client(base_url, two)

    def test_the_timeout_still_comes_from_the_caller(self) -> None:
        """逾時逐次傳而不是綁在共用的 client 上（11 §4.1 的字典由 Gateway 傳下來）。
        綁在 client 上的話，第一次呼叫的上限會變成之後所有呼叫的上限。"""
        seen: list[float | None] = []

        captured: list[httpx.Request] = []
        respond = _ok(captured)

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.extensions.get("timeout", {}).get("read"))
            # `_ok` 回傳的是未標型別的 handler（測試輔助），這裡收斂回 Response。
            return cast("httpx.Response", respond(request))

        provider = _provider(handler)
        provider.embed(["a"], model=_MODEL, timeout_seconds=1.5)
        provider.embed(["b"], model=_MODEL, timeout_seconds=9.0)

        assert seen == [1.5, 9.0]

"""驗收：OpenAI 相容的**串流對話** adapter（06 §4、02 §2、13 §3 工作包 1D-3a）。

1C-5 讓五家廠商共用一個 embedding adapter（一個實作 × 一張 `VENDORS` 表）。串流對話走
同一條路：Gemini、OpenAI、OpenRouter、NVIDIA NIM、Ollama 全部提供 OpenAI 格式的
`POST /chat/completions`，`stream=true` 時以 `data: {...}` 逐行推送。因此**不新增廠商表**
——加一家仍然是加一列。

**與 embedding adapter 分成兩個檔案**：那一份驗的是「一次請求、一次回應、順序與維度要
對」；這一份驗的是「一條連線上不斷到來的碎片，怎麼解讀、什麼時候停、壞掉的一行怎麼
辦」。串流的失敗模式完全不同，混在一個檔案裡會讓兩邊的意圖都變模糊。

**測試不打真 API**（CLAUDE.md）：全部走 `httpx.MockTransport` 餵假的 SSE 位元組。真 API
的連通性由 `make verify-provider` 手動驗。

四件事錯了不會有錯誤訊息，只會讓回答安靜地變怪或帳單變貴：

1. **`stream_options` 沒送**。OpenAI 在 `stream=true` 時**預設完全不回 usage**——不送
   這個參數的話，每一次對話的用量都要靠估算，而 2A 是拿它去跟帳單對帳的。
2. **`[DONE]` 沒認出來**。那不是 JSON，硬解會丟例外；而它是「講完了」的唯一信號。
3. **壞掉的一行**。中間的 proxy、免費額度用完的擋板、心跳註解都會混進非 JSON 的行。
   整條串流因為一行而炸掉的話，使用者看到的是講到一半突然斷掉。
4. **金鑰外洩**。它在 header 裡，而 provider 的錯誤訊息常把整個請求回貼回來——那些
   訊息會經 SSE 的 error event 直接到租戶眼前。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from ai.gateway.chat import (
    ChatMessage,
    ChatRequest,
    ChatTimeouts,
    DoneDelta,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
)
from ai.gateway.providers import ChatProvider
from ai.gateway.providers.openai_compatible import VENDORS, OpenAICompatibleChatProvider
from core.exceptions import (
    ModelNotEnabledError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

_KEY = "sk-secret-do-not-leak-1234567890"
_MODEL = "gpt-4.1-mini"
_TIMEOUTS = ChatTimeouts(connect_seconds=1.0, ttft_seconds=1.0, total_seconds=2.0)


def _request(text: str = "你好") -> ChatRequest:
    return ChatRequest(
        messages=[
            ChatMessage(role="system", content="只依據 context 回答。"),
            ChatMessage(role="user", content=text),
        ],
        model=_MODEL,
    )


def _chunk(**delta: Any) -> str:
    """一個 OpenAI 格式的串流事件。"""
    body = {"id": "chatcmpl-1", "model": _MODEL, "choices": [{"index": 0, "delta": delta}]}
    return f"data: {json.dumps(body)}\n\n"


def _finish(reason: str = "stop") -> str:
    body = {
        "id": "chatcmpl-1",
        "model": _MODEL,
        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
    }
    return f"data: {json.dumps(body)}\n\n"


def _usage_chunk(prompt: int = 11, completion: int = 7) -> str:
    """usage 事件的 `choices` 是空陣列——照 `choices[0]` 取的話會 IndexError。"""
    body = {
        "id": "chatcmpl-1",
        "model": _MODEL,
        "choices": [],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }
    return f"data: {json.dumps(body)}\n\n"


_DONE = "data: [DONE]\n\n"


def _provider(
    *,
    lines: list[str] | None = None,
    status: int = 200,
    vendor: str = "openai",
    exc: Exception | None = None,
    capture: list[httpx.Request] | None = None,
) -> OpenAICompatibleChatProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        if exc is not None:
            raise exc
        if status >= 400:
            return httpx.Response(status, json={"error": {"message": f"金鑰 {_KEY} 無效"}})
        return httpx.Response(
            200,
            content="".join(lines or []).encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )

    return OpenAICompatibleChatProvider(
        vendor=vendor,
        api_key=_KEY,
        transport=httpx.MockTransport(handler),
    )


async def _collect(provider: OpenAICompatibleChatProvider, request: ChatRequest | None = None):  # type: ignore[no-untyped-def]
    return [
        delta async for delta in provider.stream_chat(request or _request(), timeouts=_TIMEOUTS)
    ]


def _text_of(deltas: list[Any]) -> str:
    return "".join(delta.text for delta in deltas if isinstance(delta, TextDelta))


class TestRequestShape:
    async def test_it_posts_to_the_chat_completions_path(self) -> None:
        seen: list[httpx.Request] = []

        await _collect(_provider(lines=[_chunk(content="嗨"), _finish(), _DONE], capture=seen))

        assert seen[0].url.path.endswith("/chat/completions")
        assert seen[0].method == "POST"

    async def test_streaming_is_requested(self) -> None:
        seen: list[httpx.Request] = []

        await _collect(_provider(lines=[_chunk(content="嗨"), _finish(), _DONE], capture=seen))

        assert json.loads(seen[0].content)["stream"] is True

    async def test_usage_is_requested_explicitly(self) -> None:
        """**`stream_options.include_usage` 不送就完全沒有 usage**（OpenAI 的串流預設）。

        沒有它的話，每一次對話的成本都只能估——而 2A 要拿那個數字跟真帳單對帳，估算
        對不上時沒有人分得出是「我們算錯」還是「被多收了」。
        """
        seen: list[httpx.Request] = []

        await _collect(_provider(lines=[_chunk(content="嗨"), _finish(), _DONE], capture=seen))

        assert json.loads(seen[0].content)["stream_options"] == {"include_usage": True}

    async def test_messages_are_sent_verbatim_and_in_order(self) -> None:
        """system 在前、user 在後。順序顛倒的話 injection 防護的前提就沒了（10 §5：
        指令放 system、外部資料只進 context 區塊），而回答看起來仍然正常。"""
        seen: list[httpx.Request] = []

        await _collect(_provider(lines=[_chunk(content="嗨"), _finish(), _DONE], capture=seen))
        payload = json.loads(seen[0].content)

        assert [m["role"] for m in payload["messages"]] == ["system", "user"]
        assert payload["model"] == _MODEL

    async def test_the_api_key_travels_as_a_bearer_token(self) -> None:
        seen: list[httpx.Request] = []

        await _collect(_provider(lines=[_chunk(content="嗨"), _finish(), _DONE], capture=seen))

        assert seen[0].headers["Authorization"] == f"Bearer {_KEY}"

    async def test_the_timeouts_reach_the_http_layer(self) -> None:
        """連線與讀取逾時必須真的傳到 httpx（11 §4.1：timeout 字典是全域的）。

        **讀取逾時對映的是 TTFT**：串流的每一段之間都會重置讀取逾時，所以它擋的是
        「一直沒有下一個 token」，而整體上限由 Gateway 的牆鐘管。
        """
        provider = _provider(lines=[_chunk(content="嗨"), _finish(), _DONE])

        deltas = await _collect(provider)  # 不逾時的情況下照常走完

        assert _text_of(deltas) == "嗨"
        assert provider.timeout_for(_TIMEOUTS).connect == _TIMEOUTS.connect_seconds
        assert provider.timeout_for(_TIMEOUTS).read == _TIMEOUTS.ttft_seconds


class TestOptionalParameters:
    """06 §4 的兩個選配參數：`reasoning_effort` 與 `response_format`。

    **「不支援就靜默忽略」的「靜默」只有在我們不送的時候才成立。** 真的送給一個不認得
    它的相容端點，回來的是 400——那是整個請求失敗，不是忽略；而它只在切到那一家時
    才會出現，也就是最沒有人在看的時候。所以判斷必須發生在送出之前（`VendorSpec` 的
    旗標），而不是指望對方寬容。

    功能本身仍屬 backlog（06 §3.5）：這裡驗的是**欄位到參數的翻譯與降級**，不是推理
    模型答得好不好。
    """

    @staticmethod
    async def _payload_for(request: ChatRequest, *, vendor: str) -> dict[str, Any]:
        seen: list[httpx.Request] = []
        provider = _provider(
            lines=[_chunk(content="嗨"), _finish(), _DONE], vendor=vendor, capture=seen
        )
        await _collect(provider, request)
        return dict(json.loads(seen[0].content))

    async def test_off_never_appears_in_the_payload(self) -> None:
        """預設 off 表示這個鍵**根本不出現**，而不是送一個 `"off"`。

        送出去的話，有些端點會把它當成無效的列舉值而退回 400——一個預設值不該讓
        每一次普通的對話都失敗。
        """
        payload = await self._payload_for(_request(), vendor="openai")

        assert "reasoning_effort" not in payload

    async def test_a_supported_vendor_receives_it(self) -> None:
        request = dataclasses.replace(_request(), reasoning_effort="medium")

        payload = await self._payload_for(request, vendor="openai")

        assert payload["reasoning_effort"] == "medium"

    async def test_an_unverified_vendor_silently_drops_it(self) -> None:
        """**沒實測過的那家一律不送**（`VendorSpec` 的旗標預設 False）。

        填錯的代價是不對稱的：漏開一家只是少一個選配功能，多開一家是那家的**每一次**
        請求都失敗。所以旗標要靠 `make verify-provider` 實測才准打開，而不是靠文件或
        記憶——1C-5 在 `dimensions` 上就是這樣定案的。
        """
        request = dataclasses.replace(_request(), reasoning_effort="high")

        payload = await self._payload_for(request, vendor="ollama")

        assert "reasoning_effort" not in payload

    async def test_the_request_still_succeeds_when_dropped(self) -> None:
        """降級是**降級**，不是失敗：參數丟掉了，回答照樣要拿得到。"""
        request = dataclasses.replace(_request(), reasoning_effort="high")
        provider = _provider(lines=[_chunk(content="嗨"), _finish(), _DONE], vendor="ollama")

        assert _text_of(await _collect(provider, request)) == "嗨"

    async def test_response_format_follows_the_same_rule(self) -> None:
        schema: dict[str, object] = {
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": {}},
        }
        request = dataclasses.replace(_request(), response_format=schema)

        supported = await self._payload_for(request, vendor="openai")
        unverified = await self._payload_for(request, vendor="nvidia")

        assert supported["response_format"] == schema
        assert "response_format" not in unverified

    def test_every_vendor_declares_both_flags(self) -> None:
        """新增廠商時漏宣告的話，預設值（False）會讓它安靜地少掉兩個功能——這比
        送出去被退回好，但仍然要看得見。這條在加廠商時會逼人做一次決定。"""
        for vendor, spec in VENDORS.items():
            assert isinstance(spec.supports_reasoning_effort, bool), vendor
            assert isinstance(spec.supports_response_format, bool), vendor


class TestStreamParsing:
    async def test_content_chunks_become_text_deltas(self) -> None:
        provider = _provider(
            lines=[
                _chunk(content="台灣的"),
                _chunk(content="首都是"),
                _chunk(content="台北。"),
                _finish(),
                _DONE,
            ]
        )

        assert _text_of(await _collect(provider)) == "台灣的首都是台北。"

    async def test_the_done_sentinel_ends_the_stream(self) -> None:
        """`data: [DONE]` 不是 JSON。硬解會丟例外，而那個例外會在使用者看完整段回答
        之後才出現——看起來像「講完才壞掉」。"""
        provider = _provider(lines=[_chunk(content="嗨"), _finish(), _DONE])

        deltas = await _collect(provider)

        assert isinstance(deltas[-1], DoneDelta)

    async def test_the_finish_reason_is_reported(self) -> None:
        provider = _provider(lines=[_chunk(content="嗨"), _finish("length"), _DONE])

        deltas = await _collect(provider)

        assert isinstance(deltas[-1], DoneDelta)
        assert deltas[-1].finish_reason == "length"

    async def test_the_usage_chunk_has_no_choices(self) -> None:
        """usage 事件的 `choices` 是空陣列——照 `choices[0]` 取的話會 IndexError，
        而那正好發生在整段回答都送完之後。"""
        provider = _provider(lines=[_chunk(content="嗨"), _finish(), _usage_chunk(11, 7), _DONE])

        usage = [d for d in await _collect(provider) if isinstance(d, UsageDelta)]

        assert (usage[0].prompt_tokens, usage[0].completion_tokens) == (11, 7)

    async def test_blank_lines_and_comments_are_skipped(self) -> None:
        """SSE 的心跳是 `: ...` 註解行，空行是事件分隔。兩者都不是資料。"""
        provider = _provider(
            lines=[": heartbeat\n\n", "\n", _chunk(content="嗨"), _finish(), _DONE]
        )

        assert _text_of(await _collect(provider)) == "嗨"

    async def test_a_malformed_line_does_not_kill_the_stream(self) -> None:
        """中間的 proxy、擋板與訊息注入都會產生非 JSON 的行。

        整條串流因為一行而炸掉的話，使用者看到的是講到一半突然斷掉；跳過它則最多少
        一個字，而後面的內容照樣到得了。
        """
        provider = _provider(
            lines=[
                _chunk(content="嗨"),
                "data: {不是 JSON}\n\n",
                _chunk(content="你好"),
                _finish(),
                _DONE,
            ]
        )

        assert _text_of(await _collect(provider)) == "嗨你好"

    async def test_an_event_split_across_reads_is_reassembled(self) -> None:
        """**一個 JSON 事件可能跨兩次讀取到達**。

        按讀取到的位元組直接解析的話，長一點的回答會偶爾解不出來——而它與網路狀況
        有關，本機永遠不會發生。因此解析必須以「行」為單位緩衝。
        """
        raw = _chunk(content="被切成兩半的事件")
        provider = _provider(lines=[raw[: len(raw) // 2], raw[len(raw) // 2 :], _finish(), _DONE])

        assert _text_of(await _collect(provider)) == "被切成兩半的事件"

    async def test_a_stream_that_ends_without_done_is_an_error(self) -> None:
        """連線在 `[DONE]` 之前斷掉 = 回應不完整。

        當成正常結束的話，1D-4 會把一則被截斷的回答標成 completed，而使用者不會知道
        自己看到的是半篇。這裡拋出去，由 Gateway 依「有沒有吐過 token」決定形狀。
        """
        provider = _provider(lines=[_chunk(content="講到一半")])

        with pytest.raises(ProviderError):
            await _collect(provider)

    async def test_tool_call_fragments_are_assembled(self) -> None:
        """工具參數是**逐字元串流**的：`{"a` 、`":1}` 分成好幾個事件，靠 `index` 歸戶。

        不組回去就丟給上層的話，3A 拿到的是一堆解不開的 JSON 碎片；而型別現在就要對，
        1D-4 的 SSE 事件對映依它（06 §4 的 delta 五型別）。
        """
        provider = _provider(
            lines=[
                _chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "calculator", "arguments": '{"a'},
                        }
                    ]
                ),
                _chunk(tool_calls=[{"index": 0, "function": {"arguments": '":1}'}}]),
                _finish("tool_calls"),
                _DONE,
            ]
        )

        calls = [delta for delta in await _collect(provider) if isinstance(delta, ToolCallDelta)]

        assert len(calls) == 1
        assert calls[0].name == "calculator"
        assert json.loads(calls[0].arguments) == {"a": 1}


class TestErrorMapping:
    """HTTP 狀態 → 例外型別。**可否重試由型別決定**（1C-1 定案，這裡沿用）。"""

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (429, ProviderRateLimitedError),
            (401, ProviderAuthError),
            (403, ProviderAuthError),
            (404, ModelNotEnabledError),
            (500, ProviderUnavailableError),
            (503, ProviderUnavailableError),
        ],
    )
    async def test_http_status_maps_to_the_right_class(
        self, status: int, expected: type[ProviderError]
    ) -> None:
        with pytest.raises(expected):
            await _collect(_provider(status=status))

    async def test_a_context_length_error_is_not_retryable(self) -> None:
        """輸入超過模型預算（09 附錄 A 的 CONTEXT_LENGTH_EXCEEDED）重試幾次都一樣，
        而每一次都要重送整份 context——那是這條路徑上最貴的一種重試。"""
        with pytest.raises(ProviderError) as caught:
            await _collect(_provider(status=400))

        assert caught.value.retryable is False

    async def test_the_error_names_the_model_not_the_url_path(self) -> None:
        """同 embedding 那條：訊息模板是「模型未啟用：{model}」，填進 URL 路徑的話，
        使用者在 SSE 的 error event 上看到的是「模型未啟用：/chat/completions」。"""
        request = _request()

        with pytest.raises(ModelNotEnabledError) as caught:
            await _collect(_provider(status=404), request)

        assert request.model in str(caught.value)
        assert caught.value.details["model"] == request.model

    async def test_a_network_timeout_becomes_a_provider_timeout(self) -> None:
        with pytest.raises(ProviderTimeoutError):
            await _collect(_provider(exc=httpx.ReadTimeout("慢")))

    async def test_a_connection_error_is_retryable(self) -> None:
        """連不上、DNS、TLS——下一次通常就好了（Ollama 沒開是最常見的一種）。"""
        with pytest.raises(ProviderError) as caught:
            await _collect(_provider(exc=httpx.ConnectError("連不上")))

        assert caught.value.retryable is True


class TestSecretHygiene:
    @pytest.mark.parametrize("status", [400, 401, 429, 500])
    async def test_the_api_key_never_appears_in_the_error(self, status: int) -> None:
        """provider 的錯誤訊息常把整個請求回貼回來，而**這條路徑上的錯誤會經 SSE 的
        error event 直接到租戶眼前**——比 1B 的 `document.error` 更近。"""
        with pytest.raises(ProviderError) as caught:
            await _collect(_provider(status=status))

        assert _KEY not in str(caught.value)
        assert _KEY not in json.dumps(caught.value.details or {})

    def test_the_key_is_not_in_the_repr_of_the_provider(self) -> None:
        """provider 物件會整個被丟進 log 的情況很常見：設定 dump、例外的 locals。"""
        assert _KEY not in repr(_provider())


class TestVendorReuse:
    def test_it_shares_the_embedding_vendor_table(self) -> None:
        """五家的差異仍然只是位址與金鑰（1C-5 的結論）。第二張表會與第一張漂，而漏改
        的那一份只在切換到那家時才走到。"""
        from ai.gateway.providers.openai_compatible import OpenAICompatibleProvider

        assert OpenAICompatibleChatProvider(vendor="ollama")._spec is VENDORS["ollama"]
        assert OpenAICompatibleProvider(vendor="ollama")._spec is VENDORS["ollama"]

    def test_an_unknown_vendor_is_rejected(self) -> None:
        with pytest.raises(ProviderError):
            OpenAICompatibleChatProvider(vendor="not-a-vendor")


class TestProtocolCompliance:
    def test_it_satisfies_the_chat_provider_protocol(self) -> None:
        assert isinstance(_provider(), ChatProvider)

    def test_the_name_identifies_the_vendor(self) -> None:
        """`name` 會進 log 與 usage 紀錄（2A 的成本要能按 provider 分解）。"""
        assert _provider(vendor="openrouter").name == "openrouter"

    async def test_it_yields_an_async_iterator(self) -> None:
        provider = _provider(lines=[_chunk(content="嗨"), _finish(), _DONE])

        stream = provider.stream_chat(_request(), timeouts=_TIMEOUTS)

        assert isinstance(stream, AsyncIterator)
        # **關得掉**也是契約的一部分：Gateway 在呼叫端斷線時要能立刻收掉那條連線，
        # 而只有 async generator 有 `aclose`（因此 `ChatProvider` 要求的是它）。
        await stream.aclose()


class TestConnectionReuse:
    """chat 這條的每一輪對話都要重付一次握手，而那筆時間直接加在 TTFT 上。

    async client 與 embedding 那條的差別是**它綁在 event loop 上**：httpx 的連線池與
    asyncio 的 primitive 由建立它的 loop 持有，跨 loop 使用會在最不明顯的地方出錯。
    因此快取是「每個 loop 一組」，形狀同 `core/redis.py` 的 async client。
    """

    async def test_the_client_survives_a_stream(self) -> None:
        from ai.gateway.providers.openai_compatible import _shared_async_client

        provider = _provider(lines=[_chunk(content="嗨"), _DONE])
        await _collect(provider)

        client = _shared_async_client(provider._base_url, provider._transport)
        assert client.is_closed is False

    async def test_two_streams_share_one_client(self) -> None:
        from ai.gateway.providers.openai_compatible import _shared_async_client

        provider = _provider(lines=[_chunk(content="嗨"), _DONE])
        base_url, transport = provider._base_url, provider._transport

        await _collect(provider)
        first = _shared_async_client(base_url, transport)
        await _collect(provider)

        assert _shared_async_client(base_url, transport) is first

    async def test_a_different_transport_gets_its_own_client(self) -> None:
        """測試隔離：共用的話，上一條測試的假串流會餵給下一條。"""
        from ai.gateway.providers.openai_compatible import _shared_async_client

        one = _provider(lines=[_DONE])
        two = _provider(lines=[_DONE])

        assert _shared_async_client(one._base_url, one._transport) is not _shared_async_client(
            two._base_url, two._transport
        )

    async def test_the_timeouts_still_come_from_the_caller(self) -> None:
        """三層逾時逐次傳，不綁在共用的 client 上——綁上去的話，第一次串流的 TTFT
        上限會變成之後所有串流的上限。"""
        captured: list[httpx.Request] = []
        provider = _provider(lines=[_DONE], capture=captured)

        await _collect(provider)

        timeout = captured[-1].extensions.get("timeout", {})
        assert timeout["read"] == _TIMEOUTS.ttft_seconds
        assert timeout["connect"] == _TIMEOUTS.connect_seconds

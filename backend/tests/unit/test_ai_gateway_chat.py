"""驗收：AI Gateway 的串流對話路徑（06 §4、04 §5.2、13 §3 工作包 1D-3a）。

1C-1 為 embedding 立了三條規則（timeout、只重試可重試的、每次呼叫回報用量），並寫明
`stream_chat` 在 1D 沿同一個形狀補上。**但串流不是「會分很多次回來的 embedding」**，
它多出一件改變全部規則的事：**回應是逐步交付的，而交付出去就收不回來**。

由此衍生本檔要釘住的四條，每一條的反面都不會有錯誤訊息：

1. **第一個 token 是分水嶺**。還沒吐出任何東西時，換一個模型重來對呼叫端完全透明；
   已經吐出半句話之後再換，第二個模型會從頭開始講——使用者看到的是兩個開頭接在一起，
   而兩次的錢都付了。06 §4 因此明訂「僅在尚未輸出任何 token 時才切換」。
2. **失敗的形狀依分水嶺而不同**：之前是例外（呼叫端還來得及決定 HTTP 狀態），之後是
   `error` delta（HTTP 已經 200，09 §3.2）。混成一種的話，1D-4 的端點不是在串流開始後
   還想改狀態碼，就是把「一個字都沒生出來」也回成 200 + 空串流。
3. **用量一定要回得出來**，即使串到一半斷掉。token 在斷掉的那一刻已經產生費用了，
   漏記等於 2A 的成本統計把它當免費——而那種低估不會有人回報。
4. **順序是契約**：`usage` 在 `done` 之前、`done` 只出現一次而且在最後。1D-4 的 SSE
   狀態機與前端都靠這個收尾，順序漂掉時症狀是「偶爾少一個引用面板」。

**本檔不打真 API、也不碰 DB**（CLAUDE.md）：全部走假 provider，驗的是 Gateway 這一層的
行為契約。真 adapter 的 HTTP 細節在 `test_provider_chat_stream.py`。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from typing import Any

import pytest

from ai.gateway import AIGateway
from ai.gateway.chat import (
    ChatMessage,
    ChatRequest,
    ChatTimeouts,
    Delta,
    DoneDelta,
    ErrorDelta,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
)
from ai.gateway.providers import ChatProvider
from core.exceptions import (
    ErrorCode,
    ModelNotEnabledError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

_MODEL = "mock-chat"
_FAST = ChatTimeouts(connect_seconds=0.5, ttft_seconds=0.5, total_seconds=1.0)


def _request(text: str = "台灣的首都是哪裡？", *, model: str = _MODEL) -> ChatRequest:
    return ChatRequest(
        messages=[
            ChatMessage(role="system", content="只依據 context 回答。"),
            ChatMessage(role="user", content=text),
        ],
        model=model,
    )


async def _collect(stream: AsyncIterator[Delta]) -> list[Delta]:
    return [delta async for delta in stream]


def _text_of(deltas: list[Delta]) -> str:
    return "".join(delta.text for delta in deltas if isinstance(delta, TextDelta))


class _ScriptedProvider:
    """照腳本吐 delta 的假 provider。

    可設定：每個模型各要失敗幾次、第幾個 token 之後斷掉、首 token 前與 token 之間各要
    等多久。**記錄每一次被要求的模型**——fallback 有沒有真的換模型只能從這裡看出來。
    """

    name = "scripted"

    def __init__(
        self,
        *,
        chunks: tuple[str, ...] = ("台灣的", "首都是", "台北。"),
        failures: list[Exception] | None = None,
        fail_after_chunk: int | None = None,
        break_with: Exception | None = None,
        first_token_delay: float = 0.0,
        chunk_delay: float = 0.0,
        usage: tuple[int, int] | None = (12, 8),
    ) -> None:
        self.requested_models: list[str] = []
        self.closed = 0
        self._chunks = chunks
        self._failures = list(failures or [])
        self._fail_after = fail_after_chunk
        self._break_with = break_with or ProviderUnavailableError("連線在中途斷了")
        self._first_token_delay = first_token_delay
        self._chunk_delay = chunk_delay
        self._usage = usage

    async def stream_chat(
        self, request: ChatRequest, *, timeouts: ChatTimeouts
    ) -> AsyncGenerator[Any, None]:
        self.requested_models.append(request.model)
        if self._failures:
            raise self._failures.pop(0)

        try:
            if self._first_token_delay:
                await asyncio.sleep(self._first_token_delay)
            for index, chunk in enumerate(self._chunks):
                if self._fail_after is not None and index == self._fail_after:
                    raise self._break_with
                if index and self._chunk_delay:
                    await asyncio.sleep(self._chunk_delay)
                yield TextDelta(text=chunk)
            if self._usage is not None:
                prompt_tokens, completion_tokens = self._usage
                yield UsageDelta(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    model=request.model,
                )
            yield DoneDelta(finish_reason="stop")
        finally:
            # 呼叫端提前離開（client 斷線）時，async generator 被 aclose 會走到這裡。
            self.closed += 1


class TestProviderContract:
    def test_the_mock_chat_provider_satisfies_the_protocol(self) -> None:
        """MockChatProvider 是測試與本機開發的預設，它必須符合真 adapter 的同一個介面。

        不符合的話，接上真 provider 時才會發現形狀對不上——而那時 1D-4 與 1D-5 都已經
        照著 mock 的樣子寫完了（1C-1 對 embedding 的同一條，理由未變）。
        """
        from ai.gateway.providers.mock import MockChatProvider

        assert isinstance(MockChatProvider(), ChatProvider)

    async def test_the_mock_provider_is_deterministic(self) -> None:
        """同樣的請求永遠得到同樣的字。

        1D-4 的 SSE 測試要斷言「收到的內容等於這一串」，1D-5 要斷言引用標記解得出來
        ——provider 每次回不同的字時，那些測試只能斷言「有東西」，等於沒驗。
        """
        from ai.gateway.providers.mock import MockChatProvider

        gateway = AIGateway(chat_provider=MockChatProvider(), chat_timeouts=_FAST)

        first = _text_of(await _collect(gateway.stream_chat(_request())))
        second = _text_of(await _collect(gateway.stream_chat(_request())))

        assert first and first == second


class TestDeltaContract:
    """delta 的型別與順序（06 §4：text / tool_call / usage / done / error）。"""

    async def test_text_arrives_in_order(self) -> None:
        provider = _ScriptedProvider(chunks=("一", "二", "三"))
        gateway = AIGateway(chat_provider=provider, chat_timeouts=_FAST)

        deltas = await _collect(gateway.stream_chat(_request()))

        assert _text_of(deltas) == "一二三"

    async def test_done_is_last_and_appears_once(self) -> None:
        """`done` 是收尾信號。多一個的話 1D-4 的狀態機會關兩次串流；不是最後一個的話，
        前端會在 citations 還沒到之前就把面板收掉。"""
        gateway = AIGateway(chat_provider=_ScriptedProvider(), chat_timeouts=_FAST)

        deltas = await _collect(gateway.stream_chat(_request()))

        assert isinstance(deltas[-1], DoneDelta)
        assert sum(isinstance(delta, DoneDelta) for delta in deltas) == 1

    async def test_usage_precedes_done(self) -> None:
        """usage 必須在 done 之前（09 §3.2 的事件順序）。

        之後才到的話，1D-4 已經把訊息標成 completed 並回應請求了——那筆用量會寫進一個
        沒有人再讀的地方，而症狀是「有些對話沒有成本紀錄」。
        """
        gateway = AIGateway(chat_provider=_ScriptedProvider(), chat_timeouts=_FAST)

        deltas = await _collect(gateway.stream_chat(_request()))
        kinds = [type(delta) for delta in deltas]

        assert kinds.index(UsageDelta) < kinds.index(DoneDelta)

    async def test_the_finish_reason_survives(self) -> None:
        """`finish_reason` 決定 1D-4 要不要標成 interrupted，也決定 1D-5 要不要續跑
        tool 迴圈（06 §3 的 Tool Call 迴圈進入條件）。"""
        gateway = AIGateway(chat_provider=_ScriptedProvider(), chat_timeouts=_FAST)

        deltas = await _collect(gateway.stream_chat(_request()))

        assert isinstance(deltas[-1], DoneDelta)
        assert deltas[-1].finish_reason == "stop"

    async def test_tool_call_deltas_pass_through(self) -> None:
        """tool_call 是 delta 的五個型別之一（06 §4）。

        3A 才會有真的工具，但**型別現在就要對**：1D-4 的 SSE 事件對映與 05 §3.4 的
        `tool_calls jb` 欄位都依它。之後才加等於改一次 API 契約。
        """

        class _ToolProvider:
            name = "tools"

            async def stream_chat(
                self, request: ChatRequest, *, timeouts: ChatTimeouts
            ) -> AsyncGenerator[Any, None]:
                yield ToolCallDelta(
                    call_id="call_1", name="calculator", arguments='{"a":1}', status="running"
                )
                yield DoneDelta(finish_reason="tool_calls")

        gateway = AIGateway(chat_provider=_ToolProvider(), chat_timeouts=_FAST)

        deltas = await _collect(gateway.stream_chat(_request()))

        assert any(isinstance(delta, ToolCallDelta) for delta in deltas)


class TestMetering:
    """每次呼叫都要回得出 token 數——2A 計費與成本控制的原料（1C-1 定的第三條規則）。"""

    async def test_usage_is_reported(self) -> None:
        gateway = AIGateway(chat_provider=_ScriptedProvider(usage=(12, 8)), chat_timeouts=_FAST)

        usage = [
            d for d in await _collect(gateway.stream_chat(_request())) if isinstance(d, UsageDelta)
        ]

        assert usage[0].prompt_tokens == 12
        assert usage[0].completion_tokens == 8

    async def test_a_silent_provider_is_estimated_not_zeroed(self) -> None:
        """provider 不回報用量時要估一個非 0 的值（Ollama 與部分相容端點就是如此）。

        填 0 的話，2A 的成本統計會把整段對話當成免費——而那種低估不會有人回報。
        """
        gateway = AIGateway(chat_provider=_ScriptedProvider(usage=None), chat_timeouts=_FAST)

        deltas = await _collect(gateway.stream_chat(_request()))
        usage = [delta for delta in deltas if isinstance(delta, UsageDelta)]

        assert len(usage) == 1, "沒有 usage 的 provider 也必須補一筆估算"
        assert usage[0].prompt_tokens > 0 and usage[0].completion_tokens > 0

    async def test_usage_is_reported_even_when_the_stream_breaks(self) -> None:
        """斷在中途也要記帳。**那些 token 已經產生費用了**——provider 是按產出計價的，
        我們有沒有把它送到使用者面前跟要不要付錢無關。"""
        provider = _ScriptedProvider(chunks=("一", "二", "三"), fail_after_chunk=2)
        gateway = AIGateway(chat_provider=provider, chat_timeouts=_FAST, retry_backoff_seconds=())

        deltas = await _collect(gateway.stream_chat(_request()))

        assert any(isinstance(delta, UsageDelta) for delta in deltas), "中斷的串流也要有用量"

    async def test_reasoning_tokens_are_billed_as_output(self) -> None:
        """reasoning token **必須併入 output**（06 §4 明訂）。

        它是分開回報的，而它同樣按 output 計價——單獨列出但不併入的話，帳面上的
        completion_tokens 會比實際帳單少一截，且模型越新差得越多。
        """

        class _ReasoningProvider:
            name = "reasoning"

            async def stream_chat(
                self, request: ChatRequest, *, timeouts: ChatTimeouts
            ) -> AsyncGenerator[Any, None]:
                yield TextDelta(text="42")
                yield UsageDelta(
                    prompt_tokens=10,
                    completion_tokens=5,
                    reasoning_tokens=100,
                    model=request.model,
                )
                yield DoneDelta(finish_reason="stop")

        gateway = AIGateway(chat_provider=_ReasoningProvider(), chat_timeouts=_FAST)

        deltas = await _collect(gateway.stream_chat(_request()))
        usage = next(delta for delta in deltas if isinstance(delta, UsageDelta))

        assert usage.billable_output_tokens == 105


class TestRetryBeforeFirstToken:
    """分水嶺之前：可重試的錯誤照 1C-1 的規則重試，呼叫端完全看不見。"""

    async def test_a_retryable_failure_is_retried(self) -> None:
        provider = _ScriptedProvider(failures=[ProviderRateLimitedError("忙碌")])
        gateway = AIGateway(
            chat_provider=provider, chat_timeouts=_FAST, retry_backoff_seconds=(0.0,)
        )

        deltas = await _collect(gateway.stream_chat(_request()))

        assert len(provider.requested_models) == 2
        assert _text_of(deltas)

    async def test_a_non_retryable_failure_raises_immediately(self) -> None:
        """模型未啟用重試幾次都一樣（1C-1 定案）。"""
        provider = _ScriptedProvider(failures=[ModelNotEnabledError(model="nope")])
        gateway = AIGateway(
            chat_provider=provider, chat_timeouts=_FAST, retry_backoff_seconds=(0.0,)
        )

        with pytest.raises(ModelNotEnabledError):
            await _collect(gateway.stream_chat(_request()))

        assert len(provider.requested_models) == 1

    async def test_an_exhausted_chain_raises_instead_of_yielding_an_error_delta(self) -> None:
        """**一個字都還沒吐出來時，失敗是例外而不是 error delta。**

        呼叫端（1D-4）那時還沒送出任何 SSE 位元組，還來得及回一個 503（09 附錄 A 的
        PROVIDER_UNAVAILABLE）。這裡就把它變成 delta 的話，端點只剩下「HTTP 200 +
        一個只有錯誤的串流」可回，而整合方的重試邏輯看到的是成功。
        """
        provider = _ScriptedProvider(failures=[ProviderUnavailableError("掛了")] * 5)
        gateway = AIGateway(
            chat_provider=provider, chat_timeouts=_FAST, retry_backoff_seconds=(0.0, 0.0)
        )

        with pytest.raises(ProviderUnavailableError):
            await _collect(gateway.stream_chat(_request()))

    async def test_the_fallback_chain_switches_model(self) -> None:
        """primary 掛掉→換鏈上的下一個（06 §4 的 fallback 鏈）。"""
        provider = _ScriptedProvider(failures=[ProviderUnavailableError("primary 掛了")] * 3)
        gateway = AIGateway(
            chat_provider=provider,
            chat_timeouts=_FAST,
            chat_fallback_models=("backup-chat",),
            retry_backoff_seconds=(0.0,),
        )

        await _collect(gateway.stream_chat(_request()))

        assert provider.requested_models[0] == _MODEL
        assert "backup-chat" in provider.requested_models

    async def test_a_non_retryable_failure_does_not_walk_the_chain(self) -> None:
        """設定錯（模型名打錯、租戶沒開這個模型）時，鏈上的每一個都會以同樣的理由失敗
        ——走完只是把一個確定的結論延後，而每一跳都要等一次逾時。"""
        provider = _ScriptedProvider(failures=[ModelNotEnabledError(model="nope")] * 3)
        gateway = AIGateway(
            chat_provider=provider,
            chat_timeouts=_FAST,
            chat_fallback_models=("backup-chat",),
            retry_backoff_seconds=(),
        )

        with pytest.raises(ModelNotEnabledError):
            await _collect(gateway.stream_chat(_request()))

        assert provider.requested_models == [_MODEL]


class TestAfterFirstToken:
    """分水嶺之後：不重試、不換模型，把已經送出去的內容留著，以 error delta 收尾。"""

    async def test_a_break_mid_stream_is_not_retried(self) -> None:
        """已經吐出半句話再重來，第二個模型會從頭講一次——使用者看到兩個開頭黏在一起，
        而兩次的錢都付了（06 §4：僅在尚未輸出任何 token 時才切換）。"""
        provider = _ScriptedProvider(chunks=("一", "二", "三"), fail_after_chunk=2)
        gateway = AIGateway(
            chat_provider=provider,
            chat_timeouts=_FAST,
            chat_fallback_models=("backup-chat",),
            retry_backoff_seconds=(0.0, 0.0),
        )

        await _collect(gateway.stream_chat(_request()))

        assert provider.requested_models == [_MODEL], "已開始輸出就不准重試或換模型"

    async def test_the_partial_text_is_preserved(self) -> None:
        """已經交付的內容不會因為後面斷掉而消失。1D-4 要拿它去做 partial 持久化
        （06 §4 的 G-06），拿不到的話使用者重整之後那半句話就不見了。"""
        provider = _ScriptedProvider(chunks=("一", "二", "三"), fail_after_chunk=2)
        gateway = AIGateway(chat_provider=provider, chat_timeouts=_FAST, retry_backoff_seconds=())

        deltas = await _collect(gateway.stream_chat(_request()))

        assert _text_of(deltas) == "一二"

    async def test_the_stream_ends_with_an_error_delta(self) -> None:
        """HTTP 已經 200 了，錯誤只能是 event（09 §3.2）。"""
        provider = _ScriptedProvider(chunks=("一", "二", "三"), fail_after_chunk=2)
        gateway = AIGateway(chat_provider=provider, chat_timeouts=_FAST, retry_backoff_seconds=())

        deltas = await _collect(gateway.stream_chat(_request()))

        assert isinstance(deltas[-1], ErrorDelta)
        assert deltas[-1].code == ErrorCode.STREAM_INTERRUPTED
        assert deltas[-1].retryable is True

    async def test_no_done_delta_follows_an_error(self) -> None:
        """`done` 的意思是「正常講完了」。中斷後再補一個的話，1D-4 會把一則被截斷的
        回答標成 completed，而使用者不會知道自己看到的是半篇。"""
        provider = _ScriptedProvider(chunks=("一", "二", "三"), fail_after_chunk=1)
        gateway = AIGateway(chat_provider=provider, chat_timeouts=_FAST, retry_backoff_seconds=())

        deltas = await _collect(gateway.stream_chat(_request()))

        assert not any(isinstance(delta, DoneDelta) for delta in deltas)

    async def test_the_error_delta_carries_no_provider_internals(self) -> None:
        """訊息會經 SSE 直接到租戶眼前。第三方的例外字串常夾 endpoint、bucket 與帶
        金鑰的 URL（1B 結案時修過同一類問題），因此只有自家例外的訊息可以落地。"""
        provider = _ScriptedProvider(
            chunks=("一", "二"),
            fail_after_chunk=1,
            break_with=RuntimeError("https://api.example.com?key=sk-secret-1234"),
        )
        gateway = AIGateway(chat_provider=provider, chat_timeouts=_FAST, retry_backoff_seconds=())

        deltas = await _collect(gateway.stream_chat(_request()))

        assert isinstance(deltas[-1], ErrorDelta)
        assert "sk-secret-1234" not in deltas[-1].message


class TestTimeouts:
    """三層逾時（06 §4：連線 10s、TTFT 30s、整體 120s）。"""

    async def test_the_defaults_come_from_settings(self) -> None:
        """數字不寫死（鐵則 9）。它們會隨模型改變——reasoning 模型的 TTFT 天然更長，
        06 §4 已載明啟用時要覆寫。"""
        from config.settings.app_settings import get_app_settings

        settings = get_app_settings()

        assert settings.ai_chat_connect_timeout_seconds == 10.0
        assert settings.ai_chat_ttft_timeout_seconds == 30.0
        assert settings.ai_chat_total_timeout_seconds == 120.0

    async def test_a_slow_first_token_raises_a_timeout(self) -> None:
        """TTFT 逾時發生在分水嶺**之前**，所以是例外——而且它是可重試的那一種
        （provider 塞車，下一個模型通常就通了）。"""
        provider = _ScriptedProvider(first_token_delay=0.3)
        gateway = AIGateway(
            chat_provider=provider,
            chat_timeouts=ChatTimeouts(connect_seconds=0.5, ttft_seconds=0.05, total_seconds=1.0),
            retry_backoff_seconds=(),
        )

        with pytest.raises(ProviderTimeoutError) as caught:
            await _collect(gateway.stream_chat(_request()))

        assert caught.value.retryable is True

    async def test_a_slow_first_token_is_retried_or_failed_over(self) -> None:
        """TTFT 逾時是「這個模型現在很慢」，切下一個是對的處置（06 §4）。"""
        provider = _ScriptedProvider(first_token_delay=0.3)
        gateway = AIGateway(
            chat_provider=provider,
            chat_timeouts=ChatTimeouts(connect_seconds=0.5, ttft_seconds=0.05, total_seconds=1.0),
            chat_fallback_models=("backup-chat",),
            retry_backoff_seconds=(),
        )

        with pytest.raises(ProviderTimeoutError):
            await _collect(gateway.stream_chat(_request()))

        assert "backup-chat" in provider.requested_models

    async def test_the_total_timeout_ends_the_stream(self) -> None:
        """整體逾時擋的是**吐得很慢但一直沒停**的回應。

        沒有上限的話，一個壞掉的 provider 可以讓一條連線、一個 worker 與一份 quota
        保留量停在那裡好幾個小時，而監控上看起來只是「有一個請求還在跑」。
        逾時發生在分水嶺之後，所以是 error delta 而不是例外。
        """
        provider = _ScriptedProvider(chunks=tuple("一二三四五六七八九十"), chunk_delay=0.05)
        gateway = AIGateway(
            chat_provider=provider,
            chat_timeouts=ChatTimeouts(connect_seconds=0.5, ttft_seconds=0.5, total_seconds=0.12),
            retry_backoff_seconds=(),
        )

        deltas = await _collect(gateway.stream_chat(_request()))

        assert isinstance(deltas[-1], ErrorDelta)
        assert _text_of(deltas), "逾時之前已經交付的內容仍要留著"

    async def test_the_connect_timeout_reaches_the_provider(self) -> None:
        """連線逾時是 adapter 的 socket 層在管的（1C-1 定案：牆鐘只做事後分類），
        所以它必須真的傳下去，而不是只存在 Gateway 的欄位裡。"""
        seen: list[ChatTimeouts] = []

        class _RecordingProvider:
            name = "recording"

            async def stream_chat(
                self, request: ChatRequest, *, timeouts: ChatTimeouts
            ) -> AsyncGenerator[Any, None]:
                seen.append(timeouts)
                yield DoneDelta(finish_reason="stop")

        gateway = AIGateway(chat_provider=_RecordingProvider(), chat_timeouts=_FAST)

        await _collect(gateway.stream_chat(_request()))

        assert seen[0].connect_seconds == _FAST.connect_seconds


class TestCancellation:
    async def test_leaving_early_closes_the_provider_stream(self) -> None:
        """client 斷線時，底層那條 HTTP 連線必須關掉。

        不關的話它會一直掛在連線池裡直到 provider 那頭逾時——症狀是壓力測試跑到一半
        開始出現「連線池滿了」，而那時已經與斷線這件事隔了很遠。

        **注意這與 06 §4 的 G-06 不衝突**：那條說的是「client 斷線後 server 繼續收完
        該回應」，而**決定要不要繼續收的是 1D-4**（它會在背景把串流讀完並持久化）。
        Gateway 的責任只是「呼叫端真的走了的時候不留下東西」。
        """
        provider = _ScriptedProvider(chunks=("一", "二", "三"))
        gateway = AIGateway(chat_provider=provider, chat_timeouts=_FAST)

        stream = gateway.stream_chat(_request())
        assert isinstance(await stream.__anext__(), TextDelta)
        await stream.aclose()

        assert provider.closed == 1

    async def test_a_background_task_can_finish_the_stream(self) -> None:
        """**G-06 的前置條件**（06 §4）：client 斷線 → server 繼續收完該回應（成本已經
        發生）→ 完整持久化 → 進 resume buffer。

        resume buffer 與持久化本身是 1D-4 的交付物，但它們全部站在一個 Gateway 這一層
        必須成立的性質上：**串流不綁在發起它的那個 task 上**。綁住的話，FastAPI 在
        client 斷線時取消請求的 task，整條生成會跟著被取消——那時 1D-4 再怎麼寫都救不
        回來，而發現的時機會是「實作 resume 的時候才知道地基不對」。

        這裡用「原本的呼叫端讀了一段就不讀了，改由另一個 task 接手讀完」來釘住它。
        """
        provider = _ScriptedProvider(chunks=("一", "二", "三"))
        gateway = AIGateway(chat_provider=provider, chat_timeouts=_FAST)
        stream = gateway.stream_chat(_request())

        first = await stream.__anext__()  # 原呼叫端只讀到第一段就走了
        drained = await asyncio.create_task(_collect(stream))  # 換一個 task 接手

        assert isinstance(first, TextDelta)
        assert _text_of([first, *drained]) == "一二三"
        assert isinstance(drained[-1], DoneDelta), "背景收完的那一段仍要有完整的收尾"


class TestConfiguration:
    @pytest.fixture(autouse=True)
    def _reset_settings_cache(self) -> Iterator[None]:
        from config.settings.app_settings import get_app_settings

        yield
        get_app_settings.cache_clear()

    def test_the_chat_provider_comes_from_settings(self) -> None:
        from ai.gateway import build_gateway

        assert build_gateway().chat_provider_name == "mock"

    def test_embedding_only_callers_need_no_chat_provider(self) -> None:
        """ETL 的 worker 只做 embedding。硬性要求 chat provider 的話，一台完全不聊天的
        機器會因為缺 chat 設定而起不來——而它做的事一件都不需要那個設定。"""
        from ai.gateway.providers.mock import MockEmbeddingProvider

        gateway = AIGateway(embedding_provider=MockEmbeddingProvider())

        assert gateway.embed(["a"], model="mock-embedding").vectors

    async def test_calling_chat_without_a_chat_provider_fails_loudly(self) -> None:
        from ai.gateway.providers.mock import MockEmbeddingProvider

        gateway = AIGateway(embedding_provider=MockEmbeddingProvider())

        with pytest.raises(ProviderError):
            await _collect(gateway.stream_chat(_request()))

    def test_a_missing_chat_api_key_fails_at_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """與 embedding 同一條（1C-5）：延到第一次呼叫才失敗的話，服務起得來、健康檢查
        是綠的，而第一個使用者按下送出時才炸。"""
        from ai.gateway import build_gateway
        from config.settings.app_settings import get_app_settings

        monkeypatch.setenv("AI_CHAT_PROVIDER", "openai")
        monkeypatch.setenv("AI_CHAT_API_KEY", "")
        get_app_settings.cache_clear()

        with pytest.raises(ProviderError, match="AI_CHAT_API_KEY"):
            build_gateway()

    def test_an_unknown_chat_provider_does_not_fall_back_to_mock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """退回 mock 的話，正式環境會對真的使用者講假話——而那是這個產品最嚴重的一種
        故障，且沒有任何錯誤訊息會指向設定。"""
        import pydantic

        from ai.gateway import build_gateway
        from config.settings.app_settings import get_app_settings

        monkeypatch.setenv("AI_CHAT_PROVIDER", "not-a-vendor")
        get_app_settings.cache_clear()

        with pytest.raises((ProviderError, pydantic.ValidationError)):
            build_gateway()


class TestNoRealApiInTests:
    """測試永遠不打真 API（CLAUDE.md）——chat 這條路徑同樣要被強制，不能靠慣例。

    理由與 1C-5 撞到的完全一樣：`make test` 讀 repo 根的 `.env`，而在那裡設真 provider
    是使用它的正常做法。差別是 chat 比 embedding 貴得多。
    """

    def test_the_configured_chat_provider_is_mock(self) -> None:
        from config.settings.app_settings import get_app_settings

        assert get_app_settings().ai_chat_provider == "mock"

    def test_no_usable_chat_key_is_available_to_tests(self) -> None:
        from config.settings.app_settings import get_app_settings

        key = get_app_settings().ai_chat_api_key

        assert not (key and key.get_secret_value())

    def test_the_test_settings_pin_it_unconditionally(self) -> None:
        """來源層級的守門：那兩行被刪掉時，上面兩條只在**別人**的 .env 之下才會紅。"""
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2] / "config" / "settings" / "test.py"
        ).read_text(encoding="utf-8")

        assert 'os.environ["AI_CHAT_PROVIDER"] = "mock"' in source
        assert 'os.environ["AI_CHAT_API_KEY"] = ""' in source


class TestErrorCodeContract:
    def test_stream_interrupted_is_in_the_dictionary(self) -> None:
        """09 附錄 A 有 STREAM_INTERRUPTED，而 `core/exceptions.py` 是那份字典的唯一
        維護點。新增 code 視同 API 契約變更。"""
        assert ErrorCode.STREAM_INTERRUPTED == "STREAM_INTERRUPTED"

    def test_stream_interrupted_never_becomes_an_http_status(self) -> None:
        """它是 SSE event 專用（09 附錄 A 的 HTTP 欄位是「—」）——串流已經 200 了。

        對映到 HTTP 的話，`api/main.py` 會把一個「本來就不該冒到那裡」的例外變成一個
        看起來合理的回應，而真正的程式錯誤（漏接 delta）就被蓋掉了。ETL_FAILED 同理，
        1B-4 已經做過同一個決定。
        """
        from api.main import _HTTP_STATUS

        assert ErrorCode.STREAM_INTERRUPTED not in _HTTP_STATUS

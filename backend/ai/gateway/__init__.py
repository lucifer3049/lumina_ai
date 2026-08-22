"""AI Gateway —— 所有 LLM / embedding / rerank 呼叫的唯一出口（鐵則 5、06 §4）。

1C-1 落地 embedding，1D-3a 補上 `stream_chat`（`rerank` 在 2B 沿同一個形狀）。
先落地的是**規則**，不是功能面：

- **timeout**：每次對外呼叫都帶（11 §4.1）。沒有的話，provider 慢掉時 worker 會一個
  一個卡住，而症狀是「ETL 變慢」——看不出是外部依賴的問題。
- **retry**：只重試 `ProviderError.retryable` 為真的錯誤（429／5xx／逾時），退避
  1s/2s（06 §4 的 1/2/4 是給串流用的，embedding 批次短、退避不必那麼久）。配額用盡
  與模型未啟用重試幾次都一樣，立刻往上拋。
- **計量**：每次呼叫回報 token 數。它是 2A 計費與成本控制的原料，漏記等於租戶成本
  低估——而那種低估不會有人回報。

**Gateway 不碰 DB**：`ai/` 是內層（鐵則 2），usage 的落地由呼叫端的 service 負責
（usage_logs 屬 2A）。這裡只保證數字回得出來。

**串流多一條規則，而它改寫了上面三條的適用範圍**（1D-3a）：**第一個 token 是分水嶺**。
交付出去的內容收不回來，所以在它之後不准重試、不准換模型（06 §4：僅在尚未輸出任何
token 時才切換），失敗的形狀也從例外變成 `ErrorDelta`——那時 HTTP 已經 200 了。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, replace

from ai.gateway.chat import (
    ChatRequest,
    ChatTimeouts,
    Delta,
    DoneDelta,
    ErrorDelta,
    ProviderDelta,
    TextDelta,
    UsageDelta,
)
from ai.gateway.providers import ChatProvider, EmbeddingProvider
from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from core.exceptions import (
    ErrorCode,
    ProviderDimensionMismatchError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

logger = get_logger(__name__)

__all__ = ["AIGateway", "EmbedResult", "Usage", "build_gateway"]

# 退避秒數；長度 = 重試次數。空 tuple = 不重試（測試用）。
_DEFAULT_BACKOFF_SECONDS = (1.0, 2.0)

# provider 沒回報用量時的估算基準（同 providers/mock.py 與 etl/tokens.py）。
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class Usage:
    """一次呼叫的用量。embedding 沒有 output token，只有 input。"""

    prompt_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens


@dataclass(frozen=True, slots=True)
class EmbedResult:
    """Gateway 的回傳——**上層只認得這個型別**，不知道 provider 是誰。"""

    vectors: list[list[float]]
    model: str
    provider: str
    usage: Usage


@dataclass(slots=True)
class _StreamState:
    """一次 `stream_chat` 呼叫的進度。**跨重試與 fallback 存活**。

    `started` 就是分水嶺：一旦有任何東西交付給呼叫端，這次呼叫就不能重來了。
    """

    request: ChatRequest
    provider: str
    model: str = ""
    started: bool = False
    usage_seen: bool = False
    completion_chars: int = 0

    def estimated_usage(self) -> UsageDelta:
        """provider 沒回報時的估算。**寧可高估**：低估會讓成本統計看起來比實際便宜，
        而那種偏差不會有人回報（1C-1 對 embedding 的同一條）。"""
        prompt_chars = sum(len(message.content) for message in self.request.messages)
        return UsageDelta(
            prompt_tokens=max(1, prompt_chars // _CHARS_PER_TOKEN),
            completion_tokens=max(1, self.completion_chars // _CHARS_PER_TOKEN),
            model=self.model or self.request.model,
            provider=self.provider,
            estimated=True,
        )

    def interrupted(self, exc: ProviderError) -> list[Delta]:
        """中斷的收尾：（缺的話補）用量 + error。

        **用量先送**：那些 token 已經產生費用了——provider 按產出計價，我們有沒有把
        它送到使用者面前跟要不要付錢無關。
        """
        tail: list[Delta] = []
        if not self.usage_seen:
            tail.append(self.estimated_usage())
        tail.append(
            ErrorDelta(
                code=ErrorCode.STREAM_INTERRUPTED,
                # `exc` 一定是自家型別（`_iterate` 會把第三方例外收斂掉），所以訊息
                # 可以落地——第三方的原文常夾著 endpoint 與帶金鑰的 URL，而這一段會
                # 經 SSE 直接到租戶眼前。
                message=str(exc),
                # 09 附錄 A：STREAM_INTERRUPTED 一律可重試（重送整則訊息是安全的，
                # partial 由 1D-4 保存並標記）。底層的成因另記在 details 供除錯。
                retryable=True,
                details={"cause": type(exc).__name__},
            )
        )
        return tail


def _name_of(provider: object | None) -> str:
    if provider is None:
        return "unconfigured"
    return str(getattr(provider, "name", type(provider).__name__))


class AIGateway:
    """兩條路徑（embedding 同步、chat 非同步）共用一個出口。

    **兩個 provider 都是選用的**：只做 embedding 的 ETL worker 不該因為缺 chat 設定
    而起不來——它做的事一件都不需要那個設定；反過來也一樣。用到卻沒設時**明確失敗**
    （`embed` 與 `stream_chat` 各自的第一行），而不是安靜地退回某個預設。
    """

    def __init__(
        self,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        chat_provider: ChatProvider | None = None,
        timeout_seconds: float | None = None,
        chat_timeouts: ChatTimeouts | None = None,
        chat_fallback_models: tuple[str, ...] = (),
        retry_backoff_seconds: tuple[float, ...] = _DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        self._provider = embedding_provider
        self._chat = chat_provider
        self._timeout = timeout_seconds or get_app_settings().ai_embedding_timeout_seconds
        self._chat_timeouts = chat_timeouts or ChatTimeouts.from_settings()
        self._chat_fallback_models = chat_fallback_models
        self._backoff = retry_backoff_seconds

    @property
    def provider_name(self) -> str:
        return _name_of(self._provider)

    @property
    def chat_provider_name(self) -> str:
        return _name_of(self._chat)

    def embed(self, texts: list[str], *, model: str) -> EmbedResult:
        """把一批文字轉成向量。

        空批次直接回傳，不打 provider：每一次呼叫都是錢與延遲，而空批次在 1C-3 的
        worker 裡是正常情況（一份沒有 chunk 的文件）。
        """
        if self._provider is None:
            raise ProviderUnavailableError("這個 Gateway 沒有 embedding provider")
        if not texts:
            return EmbedResult(vectors=[], model=model, provider=self.provider_name, usage=Usage(0))

        embedding = self._call_with_retry(texts, model=model)
        vectors = [list(vector) for vector in embedding.vectors]
        self._check_dimensions(vectors, model=embedding.model)
        return EmbedResult(
            vectors=vectors,
            model=embedding.model,
            provider=self.provider_name,
            usage=Usage(prompt_tokens=embedding.prompt_tokens),
        )

    def _check_dimensions(self, vectors: list[list[float]], *, model: str) -> None:
        """維度不符**在這裡就攔下來**（1C-5）。

        不攔的話，錯的向量會一路走到 `EmbeddingRepository.upsert`，由 PostgreSQL 以
        ``expected 1536 dimensions, not 3072`` 擋下——那個錯誤指向 INSERT，而真正的原因
        在幾層之外（adapter 沒送 `dimensions`、或 KB 設了維度不同的模型）。更糟的是它只
        在寫入的那一刻才發生，那時這一批 embedding 的錢已經付掉了。

        **不可重試**：設定錯了，重試三次還是一樣，而每一次都要再付一批的錢。

        放在 Gateway 而不是 adapter：欄位只有一個寬度（05 §3.2），這是**跨 provider 的
        規則**，而 Gateway 是所有呼叫的唯一出口（鐵則 5）。放進 adapter 就是五份。
        """
        expected = get_app_settings().ai_embedding_dimensions
        for vector in vectors:
            if len(vector) != expected:
                raise ProviderDimensionMismatchError(
                    f"{self.provider_name} 的 {model} 回傳 {len(vector)} 維，"
                    f"但本系統的向量欄位是 {expected} 維",
                    details={
                        "provider": self.provider_name,
                        "model": model,
                        "expected": expected,
                        "actual": len(vector),
                    },
                )

    # ── 串流對話（1D-3a）──────────────────────────────────────

    async def stream_chat(self, request: ChatRequest) -> AsyncGenerator[Delta, None]:
        """跑一次生成，逐段吐回來。

        **保證四件事**，全部是上層（1D-4 的 SSE、2A 的計費）直接依賴的：

        1. `usage` 恰好一筆，且在 `done` 之前。provider 沒回報就補一筆估算值——0 會讓
           成本統計把整段對話當免費。
        2. `done` 只出現在**正常**講完時，而且是最後一筆。
        3. 第一個 token 之前的失敗是**例外**；之後的失敗是 `ErrorDelta` 收尾，且已經
           交付的內容留著（1D-4 要拿它做 partial 持久化，06 §4 的 G-06）。
        4. 分水嶺之後不重試、不換模型。

        `fallback_models` 只在分水嶺之前走：換模型等於整段重講，而使用者已經在讀第一
        個模型講到一半的內容了。
        """
        provider = self._chat
        if provider is None:
            raise ProviderUnavailableError("這個 Gateway 沒有 chat provider")

        state = _StreamState(request=request, provider=_name_of(provider))
        candidates = (request.model, *self._chat_fallback_models)
        attempts = len(self._backoff) + 1

        for model_index, model in enumerate(candidates):
            for attempt in range(attempts):
                state.model = model
                try:
                    # `aclosing` 而不是裸的 `async for`：呼叫端提前離開時，GeneratorExit
                    # 在**這一層**的 yield 被丟出來，而內層的 generator 只會等 GC 才關
                    # ——那條 HTTP 連線會一直掛在池子裡。這條鏈上的每一層都要包。
                    async with aclosing(
                        self._attempt(provider, replace(request, model=model), state)
                    ) as attempt_stream:
                        async for delta in attempt_stream:
                            yield delta
                    return
                except ProviderError as exc:
                    if state.started:
                        # 分水嶺之後：交付出去的收不回來，只能收尾。
                        logger.warning(
                            "chat_stream_interrupted",
                            provider=state.provider,
                            model=model,
                            cause=type(exc).__name__,
                        )
                        for tail in state.interrupted(exc):
                            yield tail
                        return
                    exhausted = model_index == len(candidates) - 1 and attempt == attempts - 1
                    if not exc.retryable or exhausted:
                        # 一個字都還沒吐出來 → 例外。呼叫端那時還沒送出任何 SSE
                        # 位元組，還來得及回一個 503（09 附錄 A）。
                        raise
                    # 同一個模型的重試之間才退避；**換模型不等**——慢的（或掛掉的）
                    # 是上一個模型，而使用者正在等第一個字。
                    delay = self._backoff[attempt] if attempt < len(self._backoff) else 0.0
                    logger.warning(
                        "chat_stream_retrying",
                        provider=state.provider,
                        model=model,
                        attempt=attempt + 1,
                        delay_seconds=delay,
                        cause=type(exc).__name__,
                    )
                    if delay:
                        await asyncio.sleep(delay)

    async def _attempt(
        self, provider: ChatProvider, request: ChatRequest, state: _StreamState
    ) -> AsyncGenerator[Delta, None]:
        """跑一次（一個模型、一次嘗試），並把 `usage`／`done` 的順序保證補上。

        **`done` 一到就結束**：它之後的任何東西都不該存在，而真的出現時忽略比轉發好
        ——轉發的話上層會收到「done 之後還有內容」，那是它的狀態機沒有處理的情況。
        """
        async with aclosing(self._iterate(provider, request)) as stream:
            async for delta in stream:
                if isinstance(delta, DoneDelta):
                    if not state.usage_seen:
                        yield state.estimated_usage()
                    state.started = True
                    yield delta
                    return
                if isinstance(delta, UsageDelta):
                    state.usage_seen = True
                if isinstance(delta, TextDelta):
                    state.completion_chars += len(delta.text)
                state.started = True
                yield delta

        # provider 的串流結束了卻沒有 `done`：回應不完整（adapter 通常已經先擋下，
        # 這裡是最後一道）。當成正常結束的話，1D-4 會把半篇回答標成 completed。
        raise ProviderUnavailableError(f"{state.provider} 的回應在結束前中斷")

    async def _iterate(
        self, provider: ChatProvider, request: ChatRequest
    ) -> AsyncGenerator[ProviderDelta, None]:
        """把 provider 的串流包上**牆鐘**：TTFT 與整體上限（06 §4）。

        adapter 的 socket 逾時擋不到這兩件事：`read` 逾時在每一段到達時重置，所以一個
        「每 5 秒吐一個字、永遠不停」的回應在 socket 層是完全健康的。沒有整體上限的話，
        它會佔著一條連線、一個 worker 與一份 quota 保留量到天荒地老，而監控上看起來
        只是「有一個請求還在跑」。

        逾時**逐次套在 `__anext__` 上**而不是包住整個迴圈：計時器橫跨 `yield` 的話，
        取消會被丟進**呼叫端**當時正在跑的程式碼裡，而那與這裡無關。
        """
        timeouts = self._chat_timeouts
        iterator = provider.stream_chat(request, timeouts=timeouts)
        deadline = time.monotonic() + timeouts.total_seconds
        waiting_for_first = True

        try:
            while True:
                budget = timeouts.ttft_seconds if waiting_for_first else deadline - time.monotonic()
                if budget <= 0:
                    raise ProviderTimeoutError(
                        f"{_name_of(provider)} 超過整體上限（{timeouts.total_seconds:g}s）"
                    )
                try:
                    delta = await asyncio.wait_for(iterator.__anext__(), timeout=budget)
                except StopAsyncIteration:
                    return
                except TimeoutError as exc:
                    raise self._timeout_for(provider, first=waiting_for_first) from exc
                except ProviderError:
                    raise
                except Exception as exc:
                    # adapter 沒轉譯的例外 → 我們的型別。裸的第三方例外會一路冒到
                    # SSE 的 error event，而它的訊息常夾著整個請求（含金鑰）。
                    raise ProviderUnavailableError(
                        f"{_name_of(provider)} 呼叫失敗：{type(exc).__name__}"
                    ) from exc
                waiting_for_first = False
                yield delta
        finally:
            # 呼叫端提前離開（client 斷線）時，底層那條 HTTP 連線必須跟著關掉。
            # 靠 GC 回收的話，它會一直掛在連線池裡直到對方逾時——症狀是壓測跑到一半
            # 開始「連線池滿了」，而那時已經與斷線這件事隔得很遠。
            await iterator.aclose()

    def _timeout_for(self, provider: ChatProvider, *, first: bool) -> ProviderTimeoutError:
        timeouts = self._chat_timeouts
        if first:
            return ProviderTimeoutError(
                f"{_name_of(provider)} 的第一個 token 超過 {timeouts.ttft_seconds:g}s"
            )
        return ProviderTimeoutError(
            f"{_name_of(provider)} 超過整體上限（{timeouts.total_seconds:g}s）"
        )

    # ── 內部 ────────────────────────────────────────────────

    def _call_with_retry(self, texts: list[str], *, model: str):  # type: ignore[no-untyped-def]
        attempts = len(self._backoff) + 1
        for attempt in range(attempts):
            try:
                return self._call_once(texts, model=model)
            except ProviderError as exc:
                # 不可重試（配額、模型未啟用）或次數用完 → 交給呼叫端。
                if not exc.retryable or attempt == attempts - 1:
                    raise
                delay = self._backoff[attempt]
                logger.warning(
                    "provider_call_retrying",
                    provider=self.provider_name,
                    model=model,
                    attempt=attempt + 1,
                    delay_seconds=delay,
                    cause=type(exc).__name__,
                )
                if delay:
                    time.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover —— 迴圈必然 return 或 raise

    def _call_once(self, texts: list[str], *, model: str):  # type: ignore[no-untyped-def]
        """呼叫 adapter，並把非 `ProviderError` 的例外收斂進來。

        **真正中斷呼叫的是 adapter 的 socket timeout**（`timeout_seconds` 由這裡傳入，
        理由見 providers 的 Protocol）。牆鐘只是**事後分類**：超過上限才**失敗**回來的
        呼叫，不論它自己說原因是什麼，對上層而言就是逾時——adapter 是第三方程式碼，
        它有沒有把 timeout 真的傳到底層不是我們能保證的，而分類錯的代價是把「provider
        很慢」記成「provider 壞了」，兩者的處置不同。

        **但成功的結果一律收下**，即使它遲到了。這裡的牆鐘是**事後**才量得到的（要等
        呼叫回來才知道花了多久），所以它對「掛住不回來」那種真正的危險完全沒有作用；
        它唯一攔得到的是「慢，但成功了」——而那一份向量**已經付過錢**。丟掉它換來的是
        重試再付一次，加上再等一輪的延遲，而慢的原因（provider 當下不順）通常還在。
        遲到只記一筆 warning：它是容量規劃與 provider 選型的訊號，不是這一次呼叫的失敗。
        """
        provider = self._provider
        if provider is None:  # pragma: no cover —— `embed()` 已經擋過
            raise ProviderUnavailableError("這個 Gateway 沒有 embedding provider")

        started = time.monotonic()
        try:
            embedding = provider.embed(texts, model=model, timeout_seconds=self._timeout)
        except ProviderError:
            raise
        except Exception as exc:
            raise self._classify(exc, elapsed=time.monotonic() - started) from exc

        elapsed = time.monotonic() - started
        if elapsed > self._timeout:
            logger.warning(
                "provider_call_slow",
                provider=self.provider_name,
                model=model,
                elapsed_seconds=round(elapsed, 1),
                timeout_seconds=self._timeout,
                batch_size=len(texts),
            )
        return embedding

    def _classify(self, exc: Exception, *, elapsed: float) -> ProviderError:
        """adapter 沒轉譯的例外 → 我們的型別（兩者都可重試，差別在可讀性與統計）。"""
        if elapsed > self._timeout or isinstance(exc, TimeoutError):
            return ProviderTimeoutError(self._timeout_message(elapsed))
        return ProviderUnavailableError(
            f"{self.provider_name} 呼叫失敗：{type(exc).__name__}",
        )

    def _timeout_message(self, elapsed: float) -> str:
        return f"{self.provider_name} 逾時（{elapsed:.1f}s > {self._timeout:g}s）"


def build_gateway() -> AIGateway:
    """依設定組出 Gateway（鐵則 9：provider 與模型不寫死）。

    **兩條路徑都在建立時就解析**（Fail Fast）：缺金鑰、名字打錯這類設定問題要在服務
    起來的當下就炸，而不是等第一個使用者按下送出（理由同 1A 的 JWT 金鑰）。兩者各自
    讀自己的設定，所以只用得到其中一條的部署（例如 ETL worker）不會被另一條的設定
    拖住——前提是它沒有把那一條設成需要金鑰的 provider。
    """
    settings = get_app_settings()
    return AIGateway(
        embedding_provider=_embedding_provider(settings.ai_embedding_provider),
        chat_provider=_chat_provider(settings.ai_chat_provider),
        chat_fallback_models=_fallback_models(settings.ai_chat_fallback_models),
    )


def _fallback_models(raw: str) -> tuple[str, ...]:
    """逗號分隔 → tuple。空白項丟掉（`"a,,b"`、結尾逗號都是常見的手誤，而一個空字串
    的模型名會變成一次必然失敗的 API 呼叫）。"""
    return tuple(name.strip() for name in raw.split(",") if name.strip())


def _chat_provider(name: str) -> ChatProvider:
    """名稱 → chat adapter（1D-3a）。形狀與 `_embedding_provider` 一致，理由也一致。

    未知的名稱**明確失敗**，不退回 mock。這裡的後果比 embedding 更直接：退回的話，
    正式環境會對真的使用者講出 `（mock）...` 開頭的假回答——而那是這個產品最嚴重的
    一種故障。
    """
    if name == "mock":
        from ai.gateway.providers.mock import MockChatProvider

        return MockChatProvider()

    from ai.gateway.providers.openai_compatible import VENDORS, OpenAICompatibleChatProvider

    if name not in VENDORS:
        raise ProviderUnavailableError(f"未知的 chat provider：{name}")

    settings = get_app_settings()
    spec = VENDORS[name]
    key = settings.ai_chat_api_key
    # 空字串等同沒有金鑰（理由見 `_embedding_provider`）。
    if spec.requires_api_key and not (key and key.get_secret_value()):
        raise ProviderUnavailableError(f"{name} 需要金鑰，請設定 AI_CHAT_API_KEY")

    return OpenAICompatibleChatProvider(
        vendor=name,
        api_key=key.get_secret_value() if key else None,
        base_url=settings.ai_chat_base_url or None,
    )


def _embedding_provider(name: str) -> EmbeddingProvider:
    """名稱 → adapter（1C-5）。

    未知的名稱**明確失敗**，不退回 mock：退回的話，正式環境會產出一整個知識庫的假
    向量，而症狀只是「檢索品質很差」——沒有任何錯誤訊息會指向這裡。
    """
    if name == "mock":
        from ai.gateway.providers.mock import MockEmbeddingProvider

        return MockEmbeddingProvider()

    from ai.gateway.providers.openai_compatible import VENDORS, OpenAICompatibleProvider

    if name not in VENDORS:
        raise ProviderUnavailableError(f"未知的 embedding provider：{name}")

    settings = get_app_settings()
    spec = VENDORS[name]
    key = settings.ai_embedding_api_key
    # **空字串等同沒有金鑰**：`AI_EMBEDDING_API_KEY=` 這種寫法（變數在、值是空的）
    # 是最常見的設定失誤之一，而它與完全沒設一樣不能用。只判 `is None` 的話，空值會
    # 一路送到 provider 並以 401 回來——那是一個繞了一圈才出現、且看起來像對方問題
    # 的錯誤。測試設定也靠這個行為把 `.env` 裡的真金鑰蓋掉（見 config/settings/test.py）。
    if spec.requires_api_key and not (key and key.get_secret_value()):
        # **在建立 Gateway 時就炸**，不是等第一次呼叫（Fail Fast，理由同 1A 的 JWT
        # 金鑰）。延後的話服務起得來、健康檢查是綠的，而第一份上傳的文件會在 worker
        # 裡以一個看起來像 provider 故障的錯誤失敗——然後被退避重試三次。
        raise ProviderUnavailableError(f"{name} 需要金鑰，請設定 AI_EMBEDDING_API_KEY")

    return OpenAICompatibleProvider(
        vendor=name,
        api_key=key.get_secret_value() if key else None,
        dimensions=settings.ai_embedding_dimensions,
        base_url=settings.ai_embedding_base_url or None,
    )

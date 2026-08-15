"""AI Gateway —— 所有 LLM / embedding / rerank 呼叫的唯一出口（鐵則 5、06 §4）。

1C-1 只有 embedding 這一條路徑；`stream_chat` 與 `rerank` 在 1D／2B 沿同一個形狀補上。
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
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ai.gateway.providers import EmbeddingProvider
from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from core.exceptions import ProviderError, ProviderTimeoutError, ProviderUnavailableError

logger = get_logger(__name__)

__all__ = ["AIGateway", "EmbedResult", "Usage", "build_gateway"]

# 退避秒數；長度 = 重試次數。空 tuple = 不重試（測試用）。
_DEFAULT_BACKOFF_SECONDS = (1.0, 2.0)


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


class AIGateway:
    def __init__(
        self,
        *,
        embedding_provider: EmbeddingProvider,
        timeout_seconds: float | None = None,
        retry_backoff_seconds: tuple[float, ...] = _DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        self._provider = embedding_provider
        self._timeout = timeout_seconds or get_app_settings().ai_embedding_timeout_seconds
        self._backoff = retry_backoff_seconds

    @property
    def provider_name(self) -> str:
        return str(getattr(self._provider, "name", type(self._provider).__name__))

    def embed(self, texts: list[str], *, model: str) -> EmbedResult:
        """把一批文字轉成向量。

        空批次直接回傳，不打 provider：每一次呼叫都是錢與延遲，而空批次在 1C-3 的
        worker 裡是正常情況（一份沒有 chunk 的文件）。
        """
        if not texts:
            return EmbedResult(vectors=[], model=model, provider=self.provider_name, usage=Usage(0))

        embedding = self._call_with_retry(texts, model=model)
        return EmbedResult(
            vectors=[list(vector) for vector in embedding.vectors],
            model=embedding.model,
            provider=self.provider_name,
            usage=Usage(prompt_tokens=embedding.prompt_tokens),
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
        理由見 providers 的 Protocol）。牆鐘只是**事後分類**：超過上限才回來的呼叫，
        不論它自己說失敗的原因是什麼，對上層而言就是逾時——adapter 是第三方程式碼，
        它有沒有把 timeout 真的傳到底層不是我們能保證的，而分類錯的代價是把「provider
        很慢」記成「provider 壞了」，兩者的處置不同。
        """
        started = time.monotonic()
        try:
            embedding = self._provider.embed(texts, model=model, timeout_seconds=self._timeout)
        except ProviderError:
            raise
        except Exception as exc:
            raise self._classify(exc, elapsed=time.monotonic() - started) from exc

        elapsed = time.monotonic() - started
        if elapsed > self._timeout:
            raise ProviderTimeoutError(self._timeout_message(elapsed))
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
    """依設定組出 Gateway（鐵則 9：provider 與模型不寫死）。"""
    settings = get_app_settings()
    return AIGateway(embedding_provider=_embedding_provider(settings.ai_embedding_provider))


def _embedding_provider(name: str) -> EmbeddingProvider:
    """名稱 → adapter。

    真 adapter（OpenAI／Ollama）屬 1C-5；在那之前指定它們會明確失敗，而不是安靜地
    退回 mock——退回的話，正式環境會產出一整個知識庫的假向量，而檢索結果看起來只是
    「品質很差」。
    """
    if name == "mock":
        from ai.gateway.providers.mock import MockEmbeddingProvider

        return MockEmbeddingProvider()
    raise ProviderUnavailableError(f"embedding provider 尚未實作：{name}（1C-5）")

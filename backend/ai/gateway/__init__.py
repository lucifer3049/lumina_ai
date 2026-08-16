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
from core.exceptions import (
    ProviderDimensionMismatchError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

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

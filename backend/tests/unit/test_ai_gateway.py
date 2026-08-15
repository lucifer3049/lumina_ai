"""驗收：AI Gateway 骨架與 embedding 路徑（06 §4、04 §5.2、13 §3 工作包 1C-1）。

**Gateway 是所有 LLM / embedding / rerank 呼叫的唯一出口**（鐵則 5）。它存在的理由不是
封裝美感，而是四件只能在單點做對的事：

1. **provider 可換**。模型與供應商會變（價格、政策、可用性），而換掉它們不該波及
   任何業務程式碼——上層只認得 `EmbedResult`。
2. **timeout 與 retry 有統一規則**。散在各處的話，總有一處忘了設 timeout，而那一處
   會在 provider 慢掉時把整個 threadpool 佔滿（11 §4.1）。
3. **重試的邊界**：可重試的錯誤（429、5xx、逾時）才退避重試；配額用盡與模型未啟用
   重試幾次都一樣，立刻失敗才是對的。
4. **計量**。每次呼叫的 token 數要回得出來——它是 2A 計費與成本控制的原料，漏記等於
   租戶成本低估。

本檔不碰 DB 也不打真的 provider：**LLM 呼叫一律 mock**（CLAUDE.md 測試規範）。真 provider
的 adapter 屬 1C-5，它們的驗收是「HTTP 層對得上」，與這裡的行為契約分開。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from ai.gateway import AIGateway, EmbedResult
from ai.gateway.providers import EmbeddingProvider
from core.exceptions import (
    ModelNotEnabledError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

_MODEL = "mock-embedding"


class _RecordingProvider:
    """記錄呼叫次數的假 provider；可設定前幾次要丟什麼例外。"""

    name = "recording"

    def __init__(self, *, failures: list[Exception] | None = None, dimensions: int = 8) -> None:
        self.calls: list[list[str]] = []
        self._failures = list(failures or [])
        self._dimensions = dimensions

    def embed(self, texts: list[str], *, model: str, timeout_seconds: float) -> Any:
        self.calls.append(list(texts))
        if self._failures:
            raise self._failures.pop(0)
        from ai.gateway.providers import ProviderEmbedding

        return ProviderEmbedding(
            vectors=[[0.1] * self._dimensions for _ in texts],
            model=model,
            prompt_tokens=sum(len(text) for text in texts),
        )


class _SlowProvider:
    name = "slow"

    def embed(self, texts: list[str], *, model: str, timeout_seconds: float) -> Any:
        time.sleep(timeout_seconds + 0.2)
        raise AssertionError("不該走到這裡——逾時應該先發生")


class TestProviderContract:
    def test_the_mock_provider_satisfies_the_protocol(self) -> None:
        """MockProvider 是測試與本機開發的預設，它必須符合與真 provider 相同的介面。

        不符合的話，1C-5 接上 OpenAI/Ollama 時才會發現介面對不上——而那時所有上層
        程式碼都已經照著 mock 的形狀寫完了。
        """
        from ai.gateway.providers.mock import MockEmbeddingProvider

        assert isinstance(MockEmbeddingProvider(), EmbeddingProvider)


class TestEmbed:
    def test_one_vector_per_input(self) -> None:
        provider = _RecordingProvider()

        result = AIGateway(embedding_provider=provider).embed(["一", "二", "三"], model=_MODEL)

        assert isinstance(result, EmbedResult)
        assert len(result.vectors) == 3

    def test_empty_input_never_reaches_the_provider(self) -> None:
        """空清單不打 provider。

        每一次呼叫都是錢與延遲；而空批次在 1C-3 的 worker 裡是正常情況（一份沒有
        chunk 的文件），讓它變成一次 API 呼叫只會產生噪音與帳單。
        """
        provider = _RecordingProvider()

        result = AIGateway(embedding_provider=provider).embed([], model=_MODEL)

        assert result.vectors == []
        assert provider.calls == []

    def test_usage_is_reported(self) -> None:
        """token 數要回得出來——它是 2A 計費與成本控制的原料，漏記等於租戶成本低估。"""
        provider = _RecordingProvider()

        result = AIGateway(embedding_provider=provider).embed(["hello"], model=_MODEL)

        assert result.usage.prompt_tokens > 0
        assert result.model == _MODEL
        assert result.provider == "recording"

    def test_vectors_have_a_consistent_dimension(self) -> None:
        """同一次呼叫的向量維度必須一致。

        pgvector 的欄位是定長的（05 §3.2 的 halfvec(1536)）——維度不一致的那一筆會在
        寫入時被 DB 擋下，而錯誤訊息指向 INSERT，看不出是 provider 回了不同形狀。
        """
        result = AIGateway(embedding_provider=_RecordingProvider()).embed(["a", "b"], model=_MODEL)

        assert len({len(vector) for vector in result.vectors}) == 1


class TestRetry:
    def test_retryable_errors_are_retried(self) -> None:
        """429 / 5xx / 逾時會重試——embedding 是純函式，重試安全（CLAUDE.md：retry 僅限冪等）。"""
        provider = _RecordingProvider(failures=[ProviderRateLimitedError("忙碌")])

        result = AIGateway(embedding_provider=provider, retry_backoff_seconds=(0.0, 0.0)).embed(
            ["a"], model=_MODEL
        )

        assert len(provider.calls) == 2
        assert result.vectors

    def test_retries_are_bounded(self) -> None:
        """重試次數用完就往上拋，不無限重試。

        provider 真的掛掉時無限重試會讓 worker 全部卡在同一個地方，而佇列深度看起來
        只是「處理很慢」。
        """
        provider = _RecordingProvider(failures=[ProviderUnavailableError("掛了")] * 5)

        with pytest.raises(ProviderUnavailableError):
            AIGateway(embedding_provider=provider, retry_backoff_seconds=(0.0, 0.0)).embed(
                ["a"], model=_MODEL
            )

        assert len(provider.calls) == 3  # 首次 + 兩次重試

    def test_non_retryable_errors_fail_immediately(self) -> None:
        """模型未啟用重試幾次都一樣——立刻失敗，把錯誤交給呼叫端。"""
        provider = _RecordingProvider(failures=[ModelNotEnabledError(model="nope")])

        with pytest.raises(ModelNotEnabledError):
            AIGateway(embedding_provider=provider, retry_backoff_seconds=(0.0, 0.0)).embed(
                ["a"], model=_MODEL
            )

        assert len(provider.calls) == 1


class TestTimeout:
    def test_a_slow_provider_raises_a_timeout(self) -> None:
        """**所有對外呼叫必有 timeout**（11 §4.1）。

        沒有的話，provider 慢掉時 worker 會一個一個卡住，而症狀是「ETL 變慢」——
        看不出是外部依賴的問題。
        """
        gateway = AIGateway(
            embedding_provider=_SlowProvider(),
            timeout_seconds=0.2,
            retry_backoff_seconds=(),
        )

        with pytest.raises(ProviderTimeoutError):
            gateway.embed(["a"], model=_MODEL)


class TestConfiguration:
    def test_the_default_provider_comes_from_settings(self) -> None:
        """provider 由設定決定，不是寫死（鐵則 9）。

        寫死的話，換 provider 要改程式碼；而「本機用 mock、正式用 OpenAI」這種再普通
        不過的需求會變成一個分支。
        """
        from ai.gateway import build_gateway

        gateway = build_gateway()

        assert gateway.provider_name == "mock"

    def test_the_model_name_is_not_hardcoded(self) -> None:
        """模型名稱同樣來自設定——它會隨評測結果改變（06 §3.4 的選型待 Phase 2）。"""
        from config.settings.app_settings import get_app_settings

        settings = get_app_settings()

        assert settings.ai_embedding_model
        assert settings.ai_embedding_dimensions > 0


class TestProviderSdkIsolation:
    def test_no_provider_sdk_is_imported_outside_the_gateway(self) -> None:
        """鐵則 5：provider SDK 只准出現在 `ai/gateway/providers/`。

        散出去之後，換 provider 就不再是換一個 adapter，而是全 repo 搜尋替換——而漏掉
        的那一處會在執行期才發現。這條測試掃原始碼，因為 import-linter 只管第一方套件。
        """
        from pathlib import Path

        backend = Path(__file__).resolve().parents[2]
        allowed = backend / "ai" / "gateway" / "providers"
        offenders: list[str] = []

        for path in backend.rglob("*.py"):
            if any(part in {".venv", "__pycache__", "tests"} for part in path.parts):
                continue
            if allowed in path.parents:
                continue
            source = path.read_text(encoding="utf-8")
            for sdk in ("import openai", "from openai", "import ollama", "from ollama"):
                if sdk in source:
                    offenders.append(f"{path.relative_to(backend)}: {sdk}")

        assert not offenders, f"provider SDK 洩漏到 gateway 之外：{offenders}"

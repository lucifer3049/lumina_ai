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
from collections.abc import Iterator
from typing import Any

import pytest

from ai.gateway import AIGateway, EmbedResult
from ai.gateway.providers import EmbeddingProvider
from core.exceptions import (
    ModelNotEnabledError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

_MODEL = "mock-embedding"


class _RecordingProvider:
    """記錄呼叫次數的假 provider；可設定前幾次要丟什麼例外。"""

    name = "recording"

    def __init__(
        self, *, failures: list[Exception] | None = None, dimensions: int | None = None
    ) -> None:
        self.calls: list[list[str]] = []
        self._failures = list(failures or [])
        # 預設用設定的維度。1C-5 之前這裡是 8（短向量比較好讀），但 Gateway 現在會
        # 驗維度——不合的一律擋下，而那正是 TestDimensionGuard 要的行為。
        from config.settings.app_settings import get_app_settings

        self._dimensions = (
            dimensions if dimensions is not None else get_app_settings().ai_embedding_dimensions
        )

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


class TestRealProviderWiring:
    """設定名稱 → 真 adapter（1C-5）。`build_gateway()` 是那個對照的唯一位置。"""

    @staticmethod
    def _rebuild(monkeypatch: pytest.MonkeyPatch, **env: str) -> AIGateway:
        from ai.gateway import build_gateway
        from config.settings.app_settings import get_app_settings

        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_app_settings.cache_clear()
        return build_gateway()

    @pytest.fixture(autouse=True)
    def _reset_settings_cache(self) -> Iterator[None]:
        from config.settings.app_settings import get_app_settings

        yield
        # 這一組測試會改環境變數。不清快取的話，**後面所有測試**都會拿到被改過的
        # 設定——而症狀出現在別的檔案裡，看起來與這裡無關。
        get_app_settings.cache_clear()

    @pytest.mark.parametrize("vendor", ["gemini", "openai", "openrouter", "nvidia"])
    def test_each_vendor_builds_from_settings(
        self, vendor: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gateway = self._rebuild(
            monkeypatch,
            AI_EMBEDDING_PROVIDER=vendor,
            AI_EMBEDDING_API_KEY="sk-test-key",
        )

        assert gateway.provider_name == vendor

    def test_ollama_needs_no_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """本機 Ollama 沒有金鑰概念——硬性要求會讓最容易上手的那條路走不通。"""
        gateway = self._rebuild(
            monkeypatch, AI_EMBEDDING_PROVIDER="ollama", AI_EMBEDDING_API_KEY=""
        )

        assert gateway.provider_name == "ollama"

    def test_a_missing_api_key_fails_at_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """缺金鑰要在**建立 Gateway 時**炸，不是在第一次呼叫時。

        與 1A 的 JWT 金鑰同一個道理（13 §2 未結項①的教訓）：延到第一次呼叫才失敗的話，
        服務起得來、健康檢查是綠的，而第一份上傳的文件會在 worker 裡以一個看起來像
        provider 故障的錯誤失敗——然後被重試三次。
        """
        # **設空字串而不是 delenv**：`AppSettings` 的 `env_file` 直接讀 repo 根的
        # `.env`，刪掉環境變數之後 pydantic 仍會從那個檔案讀到金鑰——開發者機器上
        # 有真金鑰時，這條測試就會安靜地失去意義（實測如此）。
        with pytest.raises(ProviderError, match="AI_EMBEDDING_API_KEY"):
            self._rebuild(monkeypatch, AI_EMBEDDING_PROVIDER="gemini", AI_EMBEDDING_API_KEY="")

    def test_an_unknown_provider_still_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """退回 mock 的話，正式環境會產出一整個知識庫的假向量，而症狀只是
        「檢索品質很差」（1C-1 已定案，這裡確認 1C-5 沒有把它改鬆）。"""
        import pydantic

        with pytest.raises((ProviderError, pydantic.ValidationError)):
            self._rebuild(monkeypatch, AI_EMBEDDING_PROVIDER="not-a-vendor")


class TestDimensionGuard:
    """維度不符要**在 Gateway 就攔下來**（1C-5）。

    不攔的話，錯的向量會一路走到 `EmbeddingRepository.upsert`，由 PostgreSQL 以
    ``expected 1536 dimensions, not 3072`` 擋下——那個錯誤指向 INSERT，而真正的原因在
    幾層之外（adapter 沒送 `dimensions`、或 KB 設了一個維度不同的模型）。更糟的是它
    只在寫入的那一刻才發生，那時 embedding 的 API 錢已經付掉了。

    放在 Gateway 而不是 adapter：這是**跨 provider 的規則**（05 §3.2 的欄位只有一個
    寬度），而 Gateway 是所有呼叫的唯一出口（鐵則 5）。放進 adapter 就是五份。
    """

    def test_a_wrong_dimension_fails_loudly(self) -> None:
        from config.settings.app_settings import get_app_settings

        configured = get_app_settings().ai_embedding_dimensions
        provider = _RecordingProvider(dimensions=configured + 1)
        gateway = AIGateway(embedding_provider=provider, retry_backoff_seconds=())

        with pytest.raises(ProviderError) as caught:
            gateway.embed(["a"], model=_MODEL)

        message = str(caught.value)
        assert str(configured) in message and str(configured + 1) in message, (
            f"錯誤訊息要同時說出「要幾維」與「拿到幾維」，否則查不出是哪邊錯：{message}"
        )

    def test_a_dimension_mismatch_is_not_retryable(self) -> None:
        """設定錯了，重試三次還是一樣——而每一次都要付一批 embedding 的錢。"""
        from config.settings.app_settings import get_app_settings

        provider = _RecordingProvider(dimensions=get_app_settings().ai_embedding_dimensions + 1)

        with pytest.raises(ProviderError) as caught:
            AIGateway(embedding_provider=provider, retry_backoff_seconds=()).embed(
                ["a"], model=_MODEL
            )

        assert caught.value.retryable is False
        assert len(provider.calls) == 1, "維度錯不該重試"

    def test_the_configured_dimension_passes(self) -> None:
        from config.settings.app_settings import get_app_settings

        configured = get_app_settings().ai_embedding_dimensions
        gateway = AIGateway(
            embedding_provider=_RecordingProvider(dimensions=configured),
            retry_backoff_seconds=(),
        )

        result = gateway.embed(["a"], model=_MODEL)

        assert len(result.vectors[0]) == configured


class TestNoRealApiInTests:
    """**測試永遠不打真 API**（CLAUDE.md 鐵則）——而這件事必須被強制，不能靠慣例。

    `make test` 帶 `--env-file ../.env`，而 `AppSettings` 讀的就是那些環境變數。所以
    只要有人在 `.env` 裡設了真 provider 與金鑰（那是使用它的**正常做法**），整個測試
    套件就會開始打真的 API。1C-5 加金鑰當天就撞到：10 條紅燈，而它們是真的送出去的
    網路請求。

    三個代價任一個都足以否決它：測試會花錢；會因為別人的服務中斷而紅；而 MockProvider
    的決定性（同樣的文字永遠得到同樣的向量）是檢索測試的前提，真 provider 沒有那性質。
    """

    def test_the_configured_provider_is_mock(self) -> None:
        from config.settings.app_settings import get_app_settings

        assert get_app_settings().ai_embedding_provider == "mock"

    def test_no_usable_api_key_is_available_to_tests(self) -> None:
        """就算哪天有人繞過上一條，也沒有東西可以拿去花。

        判「可不可用」而不是 `is None`：`AppSettings` 的 `env_file` 直接讀 repo 根的
        `.env`，刪掉環境變數蓋不掉它，只能覆寫成空字串（見 config/settings/test.py）。
        """
        from config.settings.app_settings import get_app_settings

        key = get_app_settings().ai_embedding_api_key

        assert not (key and key.get_secret_value())

    def test_the_test_settings_pin_it_unconditionally(self) -> None:
        """來源層級的守門：那三行被刪掉時，上面兩條會在**別人**的 .env 之下才紅。

        也就是說在沒設金鑰的機器上（例如 CI）刪掉它是全綠的，而問題會在下一個設了
        金鑰的人身上出現——所以這裡直接盯住那份設定檔。
        """
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2] / "config" / "settings" / "test.py"
        ).read_text(encoding="utf-8")

        assert 'os.environ["AI_EMBEDDING_PROVIDER"] = "mock"' in source
        assert 'os.environ["AI_EMBEDDING_API_KEY"] = ""' in source


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

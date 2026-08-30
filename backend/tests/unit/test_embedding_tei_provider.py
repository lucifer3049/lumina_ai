"""驗收：地端 embedding（自架 TEI + `BAAI/bge-m3`，1024 維）。

2B-4 已經把 TEI 接進 rerank；這一包把**同一套基礎設施**用在 embedding 上——同一個
映像、同一個 Blackwell tag、同一行 WSL2 的 compat 補丁。選 bge-m3 的工程理由是它與
已在跑的 `bge-reranker-v2-m3` 同家族同 tokenizer（06 §3.4 要求兩邊都必須多語）。

**這一包最容易出事的地方是「同一個名字，兩個容器」**：`tei` 現在同時是一個 rerank
provider（`RERANK_PROVIDERS`，port 8080，cross-encoder）與一個 embedding vendor
（`VENDORS`，port 8081，bi-encoder）。位址寫混的話，embedding 會打到 cross-encoder
的端點——那不是 404，是一個形狀不同的回應，而錯誤會出現在解析層。

維度從 1536 改成 1024 是**不可逆的全庫重建**（2026-08-30 人類裁決：不走 06 §2.2 的
四步並存，因為 halfvec 是固定維度欄位，1024 與 1536 塞不進同一欄）。相對的 schema
守門在 `tests/integration/test_embedding_dimensions.py`。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest

from ai.gateway.providers.openai_compatible import (
    VENDORS,
    OpenAICompatibleProvider,
    VendorSpec,
)
from config.settings.app_settings import AppSettings

# 2B-4 起 rerank 的 TEI 就住在這個 port（`app_settings.ai_rerank_base_url` 的預設）。
_RERANK_PORT = "8080"


def _response(count: int, *, dimensions: int = 8) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": [0.0] * (dimensions - 1) + [1.0]}
            for index in range(count)
        ],
        "model": "BAAI/bge-m3",
        "usage": {"prompt_tokens": 12, "total_tokens": 12},
    }


def _capture(captured: list[httpx.Request]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        count = len(json.loads(request.content)["input"])
        return httpx.Response(200, json=_response(count))

    return handler


class TestVendorTable:
    """加一家 = 加一列（`VENDORS`），不是加一個實作。"""

    def test_tei_is_in_the_vendor_table(self) -> None:
        assert "tei" in VENDORS, "VENDORS 缺 tei——設定選得到但執行期才炸"

    def test_tei_needs_no_api_key(self) -> None:
        """本機推論沒有金鑰概念。硬性要求會讓 `build_gateway()` 在 Fail Fast 那一段
        擋下一個其實完全可用的設定，而訊息會叫人去設一把不存在的金鑰。"""
        assert VENDORS["tei"].requires_api_key is False

    def test_tei_does_not_advertise_dimension_support(self) -> None:
        """bge-m3 的維度固定 1024，TEI 不吃 `dimensions` 參數——送了是整批 400。

        「支不支援」是廠商的性質，屬於這張表。寫在呼叫端的話每個呼叫端各判斷一次，
        而漏掉的那個只在切到這一家時才會壞（同 NVIDIA 與 Ollama 的理由）。
        """
        assert VENDORS["tei"].supports_dimensions is False

    def test_the_embedding_endpoint_is_not_the_rerank_port(self) -> None:
        """**`tei` 是兩個不同的容器**：rerank 的 cross-encoder 在 8080，embedding 的
        bi-encoder 在另一個 port。共用一個位址的話，`/v1/embeddings` 會打到載入
        `bge-reranker-v2-m3` 的那個容器——它不會回 404，而是一個形狀不同的回應，
        於是錯誤出現在解析層，看起來像「模型壞了」。
        """
        assert _RERANK_PORT not in VENDORS["tei"].base_url, (
            "embedding 的 tei 用了 rerank 的 port——兩個容器載的是不同的模型"
        )

    def test_the_table_carries_no_credentials(self) -> None:
        spec: VendorSpec = VENDORS["tei"]
        blob = json.dumps({"base_url": spec.base_url}).lower()
        for leak in ("sk-", "bearer ", "api-key:"):
            assert leak not in blob


class TestRequestShape:
    def test_it_does_not_send_dimensions(self) -> None:
        """TEI 收到不認得的 `dimensions` 會退整批（422）。降級是靜默的：ETL 那一批
        會整個失敗並退避重試三次，而每一次都一樣。"""
        captured: list[httpx.Request] = []

        OpenAICompatibleProvider(
            vendor="tei",
            api_key=None,
            dimensions=1024,
            transport=httpx.MockTransport(_capture(captured)),
        ).embed(["請假規定"], model="BAAI/bge-m3", timeout_seconds=5.0)

        assert "dimensions" not in json.loads(captured[0].content)

    def test_no_authorization_header_is_sent(self) -> None:
        """本機服務沒有金鑰。送一個空的 `Bearer ` 會讓部分反向代理直接 401，而那個
        失敗看起來像「TEI 掛了」。"""
        captured: list[httpx.Request] = []

        OpenAICompatibleProvider(
            vendor="tei",
            api_key=None,
            transport=httpx.MockTransport(_capture(captured)),
        ).embed(["a"], model="BAAI/bge-m3", timeout_seconds=5.0)

        assert "authorization" not in {k.lower() for k in captured[0].headers}


class TestSettings:
    def test_tei_is_selectable_as_an_embedding_provider(self) -> None:
        configurable = get_args(AppSettings.model_fields["ai_embedding_provider"].annotation)

        assert "tei" in configurable, "設定的 Literal 沒有 tei——選了它 pydantic 就先擋下"

    def test_the_default_dimension_is_1024(self) -> None:
        """設定與 `halfvec(...)` 的寬度必須是同一個數字。兩邊漂掉時 INSERT 才會炸，
        而錯誤指向寫入路徑，不指向這裡（見 `_check_dimensions` 的 docstring）。"""
        assert AppSettings.model_fields["ai_embedding_dimensions"].default == 1024

    def test_the_default_provider_is_still_mock(self) -> None:
        """預設值留在最安全的一邊：漏設環境變數時得到的是假向量，不是一個連不上的
        本機容器（同 1C-1 的理由）。接上 TEI 是部署時的選擇，不是預設。"""
        assert AppSettings.model_fields["ai_embedding_provider"].default == "mock"


class TestWiring:
    """設定名稱 → 真 adapter。`build_gateway()` 是那個對照的唯一位置。"""

    @pytest.fixture(autouse=True)
    def _reset_settings_cache(self) -> Iterator[None]:
        from config.settings.app_settings import get_app_settings

        yield
        get_app_settings.cache_clear()

    def test_tei_builds_without_an_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ai.gateway import build_gateway
        from config.settings.app_settings import get_app_settings

        monkeypatch.setenv("AI_EMBEDDING_PROVIDER", "tei")
        monkeypatch.setenv("AI_EMBEDDING_API_KEY", "")
        get_app_settings.cache_clear()

        assert build_gateway().provider_name == "tei"


class TestVerifyProviderScript:
    """`make verify-provider PROVIDER=tei CAPABILITY=embedding`（06 §3.4 的跨語言驗證）。

    2B-4 時 `tei` 只在 `RERANK_PROVIDERS` 裡，因此 `CAPABILITY=embedding` 會被那一段
    「只支援 CAPABILITY=rerank」的守門擋掉。進了 `VENDORS` 之後它應該自然通過——
    這條測試釘住的是**那個守門沒有反過來把 embedding 擋掉**。
    """

    def test_tei_is_accepted_for_both_capabilities(self) -> None:
        # 走檔案路徑而非 `import scripts.verify_provider`：`scripts/` 刻意不是
        # Python 套件（沒有 `__init__.py`），直接 import 會讓 mypy 對同一個檔案
        # 解析出兩個模組名（`verify_provider` 與 `scripts.verify_provider`），
        # `make lint-backend` 整個斷掉。形式沿用 `test_eval_runner.py` 的 runner。
        script = Path(__file__).resolve().parents[2] / "scripts" / "verify_provider.py"
        spec = importlib.util.spec_from_file_location("_verify_provider", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        assert "tei" in module.RERANK_PROVIDERS, "rerank 那一路不該因為這一包而消失"
        assert "tei" in VENDORS, "embedding 那一路走的是 VENDORS 的分支"

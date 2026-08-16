"""OpenAI 相容的 embedding adapter —— 五家共用一個實作（06 §4、02 §2、1C-5）。

Gemini、OpenAI、OpenRouter、NVIDIA NIM、Ollama **全部**提供 OpenAI 格式的
`POST /embeddings`。差別只有三件事，而三件事都是資料而不是程式：位址、要不要金鑰、
支不支援 `dimensions` 參數。因此這裡是「一個實作 × 一張表」——加一家廠商是加一列。

寫成五個檔案的代價很具體：五份會各自漂，而漏改的那一份只在切換到那家時才會走到，
也就是沒有人測的時候。**這與 02 §2 的「每家一個檔案」不同**，理由是那五個檔案會各只有
三行資料（見 13 的偏離紀錄）。

**不裝任何廠商 SDK**（鐵則 5 仍然成立——這個檔案就在 providers/ 裡）：五家是同一種
REST，直接用已有的 httpx。裝五個 SDK 是五個相依、五組版本節奏、五種認證抽象，換來的
只是同一個 POST。

**Gemini 走 OpenAI 相容端點**（`/v1beta/openai/`）而不是原生 API：原生的參數名是
`output_dimensionality`、回應形狀也不同，為了一家而多一條路徑不划算。相容端點是否
真的吃 `dimensions`，由 `make verify-provider` 實測——自動測試不打真 API。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import httpx

from ai.gateway.providers import ProviderEmbedding
from core.exceptions import (
    ModelNotEnabledError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

__all__ = ["VENDORS", "OpenAICompatibleProvider", "VendorSpec"]

# provider 沒回報用量時的估算基準（同 MockProvider 與 etl/tokens.py）。
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class VendorSpec:
    """一家廠商的全部差異。**只有位址與能力，沒有憑證**（鐵則 9）。"""

    base_url: str
    requires_api_key: bool
    # 模型維度可否由請求指定（Matryoshka 截斷）。NVIDIA 與 Ollama 的模型維度固定，
    # 送了會被退整批（400）——「支不支援」是廠商的性質，寫在呼叫端就是每個呼叫端
    # 各判斷一次，而漏掉的那個只在切到那家時才會壞。
    supports_dimensions: bool


VENDORS: dict[str, VendorSpec] = {
    # Gemini Embedding 2：預設 3072，支援 128–3072 的 Matryoshka 截斷，且截斷後會
    # 自動正規化（`gemini-embedding-001` 不會，見 `_unit`）。1536 在 MTEB 上與 3072
    # 同分，因此挑 1536 對品質沒有代價。
    "gemini": VendorSpec(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        requires_api_key=True,
        supports_dimensions=True,
    ),
    "openai": VendorSpec(
        base_url="https://api.openai.com/v1",
        requires_api_key=True,
        supports_dimensions=True,
    ),
    # 一把金鑰通到多家（含 OpenAI、Cohere、Google、Mistral 的 embedding 模型）。
    # 支不支援 `dimensions` 其實取決於底層模型，這裡取「送得出去」的一邊——不支援的
    # 模型會回 400，而那是明確的失敗，比安靜地回錯維度好。
    "openrouter": VendorSpec(
        base_url="https://openrouter.ai/api/v1",
        requires_api_key=True,
        supports_dimensions=True,
    ),
    # NVIDIA NIM：nv-embedqa-e5-v5 等模型維度固定 1024，因此在 halfvec(1536) 之下
    # 目前用不了（Gateway 的維度守門會擋，並說得出兩邊的數字）。多維度支援排後續工作包。
    "nvidia": VendorSpec(
        base_url="https://integrate.api.nvidia.com/v1",
        requires_api_key=True,
        supports_dimensions=False,
    ),
    # 本機推論，沒有金鑰概念。位址隨部署而異，由 `ai_embedding_base_url` 覆寫。
    "ollama": VendorSpec(
        base_url="http://127.0.0.1:11434/v1",
        requires_api_key=False,
        supports_dimensions=False,
    ),
}


class OpenAICompatibleProvider:
    """把一批文字送去 `/embeddings`，回傳向量、實際模型與用量。"""

    def __init__(
        self,
        *,
        vendor: str,
        api_key: str | None = None,
        dimensions: int | None = None,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if vendor not in VENDORS:
            raise ProviderUnavailableError(f"未知的 provider：{vendor}")
        self.name = vendor
        self._spec = VENDORS[vendor]
        self._base_url = base_url or self._spec.base_url
        self._dimensions = dimensions
        self._transport = transport
        # 私有且不進 __repr__（見下）。provider 物件會整個被丟進 log 的情況很常見
        # ——設定 dump、例外的 locals、除錯時的 print。
        self._api_key = api_key

    def __repr__(self) -> str:
        """**不含金鑰**。預設的 dataclass/物件 repr 會把它整串印出來。"""
        return f"OpenAICompatibleProvider(vendor={self.name!r}, base_url={self._base_url!r})"

    def embed(self, texts: list[str], *, model: str, timeout_seconds: float) -> ProviderEmbedding:
        if not texts:
            # Gateway 已經擋了一層；這裡再擋是因為 adapter 也可能被直接使用
            # （`make verify-provider`），而每一次呼叫都是錢與延遲。
            return ProviderEmbedding(vectors=[], model=model, prompt_tokens=0)

        payload: dict[str, Any] = {"model": model, "input": list(texts)}
        if self._dimensions is not None and self._spec.supports_dimensions:
            # **不送的話 Gemini 回 3072**，而欄位是 halfvec(1536)：寫入會被 DB 擋下，
            # 而錯誤指向 INSERT——看不出原因在幾層之外一個沒送出去的參數。
            payload["dimensions"] = self._dimensions

        data = self._post(payload, timeout_seconds=timeout_seconds)
        return self._parse(data, texts=texts, requested_model=model)

    # ── HTTP ────────────────────────────────────────────────

    def _post(self, payload: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                # 11 §4.1：timeout 由 Gateway 傳入（全域字典才有意義）。沒有它的話，
                # provider 慢掉時 worker 會一個一個卡住，而症狀是「ETL 變慢」。
                timeout=httpx.Timeout(timeout_seconds),
            ) as client:
                response = client.post("/embeddings", json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"{self.name} 逾時（{timeout_seconds:g}s）") from exc
        except httpx.HTTPError as exc:
            # 連不上、DNS、TLS——下一次通常就好了（Ollama 沒開是最常見的一種）。
            raise ProviderUnavailableError(f"{self.name} 連線失敗：{type(exc).__name__}") from exc

        if response.status_code >= 400:
            raise self._error_for(response)

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderUnavailableError(f"{self.name} 回應不是 JSON") from exc
        if not isinstance(body, dict):
            raise ProviderUnavailableError(f"{self.name} 回應格式非預期")
        return body

    def _error_for(self, response: httpx.Response) -> ProviderError:
        """HTTP 狀態 → 我們的例外型別。**可否重試由型別決定**（1C-1 定案）。

        **訊息一律由我們自己組**，絕不回貼 provider 的原文（鐵則 9）：那些訊息常把整個
        請求（含 Authorization 標頭或帶 key 的 URL）回貼回來，而 `document.error` 會經
        `DocumentOut` 回到租戶手上。狀態碼留著——分類與統計要用它，而它不洩漏內容。
        """
        status = response.status_code
        detail = {"status": status, "vendor": self.name}

        if status == 429:
            return ProviderRateLimitedError(f"{self.name} 頻率限制", details=detail)
        if status in (401, 403):
            return ProviderAuthError(f"{self.name} 拒絕了我們的憑證", details=detail)
        if status == 404:
            # 模型不存在／那家沒有這個模型。1C-5 之後最可能的設定錯誤——KB 的
            # `embedding_model` 是 per-KB 的，而 provider 是全域的，兩者對不上就走到這。
            return ModelNotEnabledError(model=str(response.request.url.path))
        if status == 400:
            # 參數被退（例如對固定維度的模型送了 dimensions）。重試不會有不同結果。
            return ModelNotEnabledError(model=str(response.request.url.path))
        return ProviderUnavailableError(f"{self.name} 回應 {status}", details=detail)

    # ── 解析 ────────────────────────────────────────────────

    def _parse(
        self, body: dict[str, Any], *, texts: list[str], requested_model: str
    ) -> ProviderEmbedding:
        rows = body.get("data")
        if not isinstance(rows, list) or len(rows) != len(texts):
            # 回 200 但形狀不對：proxy 插了一頁 HTML、免費額度用完回了別的東西。
            # 裸的 KeyError 會冒到 worker 頂層變成一個看不出所以然的例外。
            raise ProviderUnavailableError(
                f"{self.name} 回傳 {len(rows) if isinstance(rows, list) else '非預期'} 筆，"
                f"送出 {len(texts)} 筆"
            )

        try:
            ordered = _ordered(rows)
            vectors = [_unit([float(value) for value in row["embedding"]]) for row in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderUnavailableError(f"{self.name} 的回應缺少必要欄位") from exc

        return ProviderEmbedding(
            vectors=vectors,
            # provider 回報的實際模型（別名解析後可能不同）；沒回報就沿用請求值。
            model=str(body.get("model") or requested_model),
            prompt_tokens=_usage_tokens(body, texts),
        )


def _ordered(rows: list[Any]) -> list[Any]:
    """把回應的列排成**送出時的順序**。

    順序不保證等於送出順序，所以 OpenAI 的格式帶 `index`。照陣列位置收的話，
    `EmbeddingService` 的 `zip` 會把 A 的向量配給 B 的 chunk：每一筆都合法、每一筆都
    掛在錯的內容上，而事後沒有任何辦法發現。

    **`index` 為 0 時可能整個不出現**（2026-08-16 實測 Gemini 相容端點：陣列位置 0
    沒有 `index`，位置 1、2 有）。那是 protobuf 系列化省略預設值的慣例，不是壞掉的
    回應——嚴格要求每一列都有 `index` 會讓 Gemini 完全用不了，而那正是 1C-5 第一次
    實測撞到的兩個錯誤。因此**缺席一律當成 0**。

    真正的把關是最後那一步：解出來的 index 必須恰好是 ``0..n-1`` 的一個排列。它不管
    廠商用哪種慣例，只問「這組編號能不能唯一決定順序」——不能的話就是錯誤，而不是
    猜一個。完全沒有 `index` 的回應（若哪天有廠商如此）會落在這裡，退回陣列順序。
    """
    if not all(isinstance(row, dict) for row in rows):
        raise ProviderUnavailableError("回應的列不是物件")

    explicit = any("index" in row for row in rows)
    resolved = [int(row.get("index", 0)) for row in rows]

    if sorted(resolved) == list(range(len(rows))):
        paired = sorted(zip(resolved, rows, strict=True), key=lambda pair: pair[0])
        return [row for _, row in paired]
    if not explicit:
        # 一列都沒有 `index`：唯一能做的假設是「陣列順序 = 送出順序」，那也是所有
        # OpenAI 相容 client 的實際行為。
        return rows
    raise ProviderUnavailableError("回應的 index 無法唯一決定順序")


def _unit(vector: list[float]) -> list[float]:
    """正規化成單位長度。

    `gemini-embedding-001` 的 Matryoshka 截斷**不會**自動正規化（`gemini-embedding-2`
    會）。cosine 距離本身對長度不敏感，所以這不影響今天的排序——但它讓所有 provider
    產出的向量性質一致（與 MockProvider 相同），而 05 §5.3 若改用內積 ops，沒有這一步
    的排序會變，且變得毫無徵兆。
    """
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def _usage_tokens(body: dict[str, Any], texts: list[str]) -> int:
    """用量。provider 沒回報時**估一個非 0 的值**（1C-1 的 Protocol 已載明）。

    填 0 會讓 2A 的成本統計把這次呼叫當成免費，而那種低估不會有人回報。
    """
    usage = body.get("usage")
    if isinstance(usage, dict):
        reported = usage.get("prompt_tokens") or usage.get("total_tokens")
        if isinstance(reported, int) and reported > 0:
            return reported
    return max(1, sum(len(text) for text in texts) // _CHARS_PER_TOKEN)

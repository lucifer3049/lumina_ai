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

import json
import math
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from ai.gateway.chat import (
    ChatRequest,
    ChatTimeouts,
    DoneDelta,
    ProviderDelta,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
)
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
    # ── chat 的兩個選配參數（06 §4 的介面預留）───────────────────
    #
    # **預設一律 False，而那不是「那家做不到」，是「我們還沒實測過」。**
    # 06 §4 定的降級模式是「不支援的 provider 靜默忽略」——而「靜默」只有在**我們**
    # 不送的時候才成立：真的送給一個不認得它的端點，回來的是 400，那是整個請求失敗，
    # 不是忽略。所以判斷必須發生在送出之前，也就是這張表。
    #
    # 開成 True 的條件是 `make verify-provider CAPABILITY=chat --reasoning/--json` 對
    # 那一家實測通過（同 `supports_dimensions` 的定案方式：那個旗標當初也是實測才敢
    # 寫死的）。憑記憶或憑文件填 True 的代價是不對稱的——填錯成 False 只是少一個
    # 選配功能，填錯成 True 是那家的每一次請求都失敗。
    supports_reasoning_effort: bool = False
    supports_response_format: bool = False


VENDORS: dict[str, VendorSpec] = {
    # Gemini Embedding 2：預設 3072，支援 128–3072 的 Matryoshka 截斷，且截斷後會
    # 自動正規化（`gemini-embedding-001` 不會，見 `_unit`）。1536 在 MTEB 上與 3072
    # 同分，因此挑 1536 對品質沒有代價。
    "gemini": VendorSpec(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        requires_api_key=True,
        supports_dimensions=True,
    ),
    # **唯一兩個旗標開著的**：`reasoning_effort` 與 `response_format` 就是 OpenAI 自己
    # 定義的參數，其餘四家是「相容端點」——相容的是哪幾個欄位由那家決定，而那正是
    # 1C-5 在 `dimensions` 上實測到差異的地方（Gemini 吃、NVIDIA 與 Ollama 不吃）。
    "openai": VendorSpec(
        base_url="https://api.openai.com/v1",
        requires_api_key=True,
        supports_dimensions=True,
        supports_reasoning_effort=True,
        supports_response_format=True,
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


class _VendorClient:
    """廠商解析與憑證處理 —— embedding 與 chat 兩個 adapter 共用。

    **共用的是「哪一家、位址在哪、金鑰怎麼帶」**，不是呼叫本身：那三件事在兩條路徑上
    完全相同，而寫兩份的話，加一家廠商就有一半的機會只改到一邊——症狀是「embedding
    切過去了、聊天還在打舊位址」，而兩邊都不會報錯。
    """

    def __init__(self, *, vendor: str, api_key: str | None, base_url: str | None) -> None:
        if vendor not in VENDORS:
            raise ProviderUnavailableError(f"未知的 provider：{vendor}")
        self.name = vendor
        self._spec = VENDORS[vendor]
        self._base_url = base_url or self._spec.base_url
        # 私有且不進 __repr__（見下）。provider 物件會整個被丟進 log 的情況很常見
        # ——設定 dump、例外的 locals、除錯時的 print。
        self._api_key = api_key

    def __repr__(self) -> str:
        """**不含金鑰**。預設的 dataclass/物件 repr 會把它整串印出來。"""
        return f"{type(self).__name__}(vendor={self.name!r}, base_url={self._base_url!r})"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers


class OpenAICompatibleProvider(_VendorClient):
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
        super().__init__(vendor=vendor, api_key=api_key, base_url=base_url)
        self._dimensions = dimensions
        self._transport = transport

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
        headers = self._headers()

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
            raise _error_for(response, vendor=self.name)

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderUnavailableError(f"{self.name} 回應不是 JSON") from exc
        if not isinstance(body, dict):
            raise ProviderUnavailableError(f"{self.name} 回應格式非預期")
        return body

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


class OpenAICompatibleChatProvider(_VendorClient):
    """把一次生成請求送去 `/chat/completions`，並把回來的位元組翻成 delta（1D-3a）。

    **同一張 `VENDORS` 表**：五家的 `/chat/completions` 也都是 OpenAI 格式，差別仍然
    只有位址與金鑰。第二張表會與第一張漂，而漏改的那一份只在切到那家時才走得到。

    這裡只做「翻譯」：重試、fallback、牆鐘逾時與用量補估都在 Gateway（見
    `ChatProvider` 的 docstring）。
    """

    def __init__(
        self,
        *,
        vendor: str,
        api_key: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(vendor=vendor, api_key=api_key, base_url=base_url)
        self._transport = transport

    def timeout_for(self, timeouts: ChatTimeouts) -> httpx.Timeout:
        """三層逾時 → httpx 的四個旋鈕。

        **`read` 對映的是 TTFT 而不是整體上限**：串流的每一段到達都會重置 read 逾時，
        所以它擋的是「一直沒有下一個 token」——正是 TTFT 要擋的那件事。整體上限沒有
        對應的 httpx 旋鈕（httpx 沒有「整條回應的總時長」），由 Gateway 的牆鐘管。

        寫 `connect` 而不是一個總 timeout：連不上（對方沒開、DNS 壞了）通常一兩秒內
        就知道結果，讓它等 30 秒只是把一個確定的失敗拖慢，而 fallback 在後面排隊。
        """
        return httpx.Timeout(
            connect=timeouts.connect_seconds,
            read=timeouts.ttft_seconds,
            write=timeouts.connect_seconds,
            pool=timeouts.connect_seconds,
        )

    async def stream_chat(
        self, request: ChatRequest, *, timeouts: ChatTimeouts
    ) -> AsyncGenerator[ProviderDelta, None]:
        payload = self._payload(request)
        try:
            async with (
                httpx.AsyncClient(
                    base_url=self._base_url,
                    transport=self._transport,
                    timeout=self.timeout_for(timeouts),
                ) as client,
                client.stream(
                    "POST", "/chat/completions", json=payload, headers=self._headers()
                ) as response,
            ):
                if response.status_code >= 400:
                    # 讀完才拿得到 `response.request`／狀態以外的東西；內容一律不落地
                    # （見 `_error_for`），讀它只是為了讓連線乾淨地結束。
                    await response.aread()
                    raise _error_for(response, vendor=self.name)

                async for delta in _parse_stream(response.aiter_lines(), vendor=self.name):
                    yield delta
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"{self.name} 逾時") from exc
        except httpx.HTTPError as exc:
            # 連不上、DNS、TLS、連線中途斷掉——下一次通常就好了。
            raise ProviderUnavailableError(f"{self.name} 連線失敗：{type(exc).__name__}") from exc

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            "stream": True,
            # **不送這個就完全沒有 usage**：OpenAI 在 `stream=true` 時預設不回用量。
            # 漏送不會有任何錯誤，只會讓每一次對話的成本都變成估算值——而 2A 是拿它
            # 去跟真帳單對帳的，估算對不上時沒有人分得出是我們算錯還是被多收了。
            "stream_options": {"include_usage": True},
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        # 兩個選配參數：**那家沒實測過就不送**（06 §4 的「不支援就靜默忽略」）。
        # 送出去才被 400 退回的話，失敗的是整個請求——那不是忽略，是故障，而且只在
        # 切到那一家時才會出現。旗標的定義見 `VendorSpec`。
        if request.reasoning_effort != "off" and self._spec.supports_reasoning_effort:
            payload["reasoning_effort"] = request.reasoning_effort
        if request.response_format is not None and self._spec.supports_response_format:
            payload["response_format"] = request.response_format
        return payload


async def _parse_stream(
    lines: AsyncIterator[str], *, vendor: str
) -> AsyncGenerator[ProviderDelta, None]:
    """SSE 的位元組流 → delta。

    **`done` 押到最後才吐**，即使 `finish_reason` 早就到了：OpenAI 的順序是
    「finish_reason 的事件 → usage 的事件 → [DONE]」，照收到的順序轉發的話 `usage`
    會排在 `done` 之後，而那違反 Gateway 對上層的順序保證（1D-4 靠它收尾）。
    """
    tool_calls: dict[int, dict[str, str]] = {}
    usage: dict[str, int] | None = None
    finish_reason: str | None = None
    model: str = ""
    completed = False

    async for raw in lines:
        line = raw.rstrip("\r")
        # 空行是事件分隔，`:` 開頭是註解（SSE 的心跳就長這樣）。兩者都不是資料。
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload == "[DONE]":
            completed = True
            break
        try:
            event = json.loads(payload)
        except ValueError:
            # 中間的 proxy、免費額度用完的擋板都會塞進非 JSON 的行。整條串流因為一行
            # 而炸掉的話，使用者看到的是講到一半突然斷掉；跳過它最多少一個字。
            continue
        if not isinstance(event, dict):
            continue

        model = str(event.get("model") or model)
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]

        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            # usage 事件的 `choices` 是**空陣列**——照 `choices[0]` 取會 IndexError，
            # 而它正好發生在整段回答都送完之後。
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            continue
        if choice.get("finish_reason"):
            finish_reason = str(choice["finish_reason"])

        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str) and content:
            yield TextDelta(text=content)
        _accumulate_tool_calls(tool_calls, delta.get("tool_calls"))

    if not completed and finish_reason is None:
        # 連線在講完之前就斷了。當成正常結束的話，1D-4 會把一則被截斷的回答標成
        # completed，而使用者不會知道自己看到的是半篇。
        raise ProviderUnavailableError(f"{vendor} 的回應在結束前中斷")

    for call in tool_calls.values():
        yield ToolCallDelta(
            call_id=call.get("id", ""),
            name=call.get("name", ""),
            arguments=call.get("arguments", ""),
            status="done",
        )
    if usage is not None:
        # **`reasoning_tokens` 留 0 是對的，不是漏掉**：OpenAI 格式的
        # `completion_tokens` 已經含推理 token（`completion_tokens_details` 只是它的
        # 明細），而 `UsageDelta.reasoning_tokens` 的口徑是「尚未計入 completion 的
        # 部分」。照抄明細會讓 `billable_output_tokens` 把同一批 token 加第二次，
        # 而那個高估在推理模型上可以是好幾倍。
        yield UsageDelta(
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            model=model,
            provider=vendor,
        )
    yield DoneDelta(finish_reason=finish_reason or "stop")


def _accumulate_tool_calls(sink: dict[int, dict[str, str]], fragments: Any) -> None:
    """把逐字元串流的工具參數依 `index` 歸戶並串起來。

    不組回去就往上丟的話，上層拿到的是一堆解不開的 JSON 碎片——而 `index` 是唯一能
    分辨「同一個呼叫的下一段」與「另一個呼叫」的東西（同一則回應可以有多個呼叫）。
    """
    if not isinstance(fragments, list):
        return
    for fragment in fragments:
        if not isinstance(fragment, dict):
            continue
        index = int(fragment.get("index", 0))
        call = sink.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if fragment.get("id"):
            call["id"] = str(fragment["id"])
        function = fragment.get("function")
        if not isinstance(function, dict):
            continue
        if function.get("name"):
            call["name"] = str(function["name"])
        if isinstance(function.get("arguments"), str):
            call["arguments"] += function["arguments"]


def _error_for(response: httpx.Response, *, vendor: str) -> ProviderError:
    """HTTP 狀態 → 我們的例外型別。**可否重試由型別決定**（1C-1 定案）。

    **訊息一律由我們自己組**，絕不回貼 provider 的原文（鐵則 9）：那些訊息常把整個
    請求（含 Authorization 標頭或帶 key 的 URL）回貼回來，而它們會經 `document.error`
    或 SSE 的 error event 回到租戶手上。狀態碼留著——分類與統計要用它，而它不洩漏內容。
    """
    status = response.status_code
    detail = {"status": status, "vendor": vendor}

    if status == 429:
        return ProviderRateLimitedError(f"{vendor} 頻率限制", details=detail)
    if status in (401, 403):
        return ProviderAuthError(f"{vendor} 拒絕了我們的憑證", details=detail)
    if status == 404:
        # 模型不存在／那家沒有這個模型。1C-5 之後最可能的設定錯誤——KB 的
        # `embedding_model` 是 per-KB 的，而 provider 是全域的，兩者對不上就走到這。
        return ModelNotEnabledError(model=str(response.request.url.path))
    if status == 400:
        # 參數被退（固定維度的模型送了 dimensions、context 超過模型上限）。
        # 重試不會有不同結果，而在 chat 這條路徑上，重試要重送整份 context——
        # 那是整條讀路徑上最貴的一種重試。
        return ModelNotEnabledError(model=str(response.request.url.path))
    return ProviderUnavailableError(f"{vendor} 回應 {status}", details=detail)


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

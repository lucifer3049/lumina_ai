"""Provider adapter 的介面（06 §4：統一介面，上層不知道 provider 差異）。

**這是 repo 內唯一准許出現 provider SDK 的地方**（鐵則 5）。介面刻意訂得很窄——
adapter 只負責「把我們的請求翻成它家的呼叫，再把回應翻回來」，重試、逾時、計量、
fallback 全在 Gateway。分散到 adapter 的話，每接一家就要重寫一次那些規則，而其中
一家寫錯不會有人發現（它只在那家 provider 出問題時才走到）。

`Protocol` 而不是抽象基底類別：adapter 不必繼承我們的東西，只要形狀對。這讓測試用的
假 provider 可以是十行的 class，而不是被迫扛一個基底類別的建構子。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ai.gateway.chat import ChatRequest, ChatTimeouts, ProviderDelta


@dataclass(frozen=True, slots=True)
class ProviderEmbedding:
    """adapter 的回傳：向量、實際使用的模型、input token 數。

    ``model`` 由 adapter 回報而不是沿用請求值：provider 會做別名解析
    （``text-embedding-3-small`` → 帶日期的實際版本），而 05 §3.2 的
    ``UNIQUE(chunk_id, model, embedding_version)`` 要記的是**真的被用到的那一個**。

    ``prompt_tokens`` 是計費原料。provider 沒回報時由 adapter 估算並照實填 0 以外的
    值——0 會讓 2A 的成本統計把這次呼叫當成免費。
    """

    vectors: list[list[float]]
    model: str
    prompt_tokens: int


@runtime_checkable
class EmbeddingProvider(Protocol):
    """embedding adapter。

    ``timeout_seconds`` 由 Gateway 傳入而不是 adapter 自己決定：11 §4.1 的 timeout
    字典是全域的，adapter 各自訂一個值會讓那份字典失去意義。
    """

    name: str

    def embed(
        self, texts: list[str], *, model: str, timeout_seconds: float
    ) -> ProviderEmbedding: ...


@runtime_checkable
class ChatProvider(Protocol):
    """串流對話 adapter（1D-3a）。

    介面比 embedding 更窄：**adapter 只管「把位元組翻成 delta」**。重試、fallback、
    牆鐘逾時、用量補估全在 Gateway——散到 adapter 的話，每接一家就要重寫一次那些規則，
    而其中一家寫錯只在切換到那家時才走得到，也就是沒有人測的時候。

    **失敗一律用例外表達**，不吐 `ErrorDelta`：只有 Gateway 知道現在在不在分水嶺之後
    （第一個 token 有沒有交付出去），而那件事決定失敗該長成例外還是 delta。

    `timeouts` 由 Gateway 傳入（同 embedding 的 `timeout_seconds`）：11 §4.1 的 timeout
    字典是全域的，adapter 各自訂一個值會讓那份字典失去意義。adapter 負責的是 socket
    層（連線與「下一段一直不來」），整體上限由 Gateway 的牆鐘管——adapter 是第三方
    程式碼，它有沒有把 timeout 真的傳到底層不是我們能保證的。
    """

    name: str

    def stream_chat(
        self, request: ChatRequest, *, timeouts: ChatTimeouts
    ) -> AsyncGenerator[ProviderDelta, None]: ...


@dataclass(frozen=True, slots=True)
class RerankedDocument:
    """rerank 的一筆結果：**原始清單的索引**與 cross-encoder 給的分數。

    回索引而不是回重排後的文字：呼叫端手上是 `RetrievedChunk`（帶 chunk_id、頁碼、
    檔名、doc_version），只拿文字回來的話那些欄位就對不回去了——而引用要靠它們說出
    「這句話出自哪份文件第幾頁」。
    """

    index: int
    score: float


@dataclass(frozen=True, slots=True)
class ProviderRerank:
    """adapter 的回傳。``model`` 由 adapter 回報（同 embedding：provider 會做別名解析，
    而「當時用了哪個模型」是 06 §1 版本化貫穿要留的快照）。"""

    results: list[RerankedDocument]
    model: str


@runtime_checkable
class RerankProvider(Protocol):
    """rerank adapter（2B-3）。

    **這個介面不共用 `openai_compatible.py`**（13 §4 的 2B 開工前定案）：rerank 沒有
    OpenAI 相容的共通形狀，TEI 的 `/rerank`、Cohere、Jina、NVIDIA 各一套 request 與
    response。1C-5「五家共用一個 adapter」的紅利在這裡不成立。

    介面因此只說「我們要什麼」——一組 (原始索引, 分數)，由各家 adapter 自己翻譯。
    `timeout_seconds` 同樣由 Gateway 傳入（11 §4.1 的 timeout 字典是全域的）。
    """

    name: str

    def rerank(
        self, query: str, documents: list[str], *, model: str, timeout_seconds: float
    ) -> ProviderRerank: ...

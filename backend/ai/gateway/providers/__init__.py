"""Provider adapter 的介面（06 §4：統一介面，上層不知道 provider 差異）。

**這是 repo 內唯一准許出現 provider SDK 的地方**（鐵則 5）。介面刻意訂得很窄——
adapter 只負責「把我們的請求翻成它家的呼叫，再把回應翻回來」，重試、逾時、計量、
fallback 全在 Gateway。分散到 adapter 的話，每接一家就要重寫一次那些規則，而其中
一家寫錯不會有人發現（它只在那家 provider 出問題時才走到）。

`Protocol` 而不是抽象基底類別：adapter 不必繼承我們的東西，只要形狀對。這讓測試用的
假 provider 可以是十行的 class，而不是被迫扛一個基底類別的建構子。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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

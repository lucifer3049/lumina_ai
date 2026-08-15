"""MockProvider —— 測試與本機開發的預設 embedding provider。

**LLM 呼叫在測試裡一律 mock**（CLAUDE.md）。但這個 mock 不是「回一堆零」：向量由內容
的雜湊決定，因此具備兩個真 embedding 才有的性質，而 1C-4 的檢索測試需要它們：

1. **決定性**——同樣的文字永遠得到同樣的向量。少了它，「重跑 ETL 之後檢索結果一樣」
   這種測試會隨機紅。
2. **相異性**——不同的文字得到不同的方向。全部回同一個向量的話，任何檢索測試都會
   通過（每一筆的相似度都相同），那種綠燈毫無意義。

它**不具備**語意相似性：意思相近的兩句話不會比較靠近。所以檢索**品質**的評測要等真
模型與 Phase 2 的 golden set，這裡只驗機制。
"""

from __future__ import annotations

import hashlib
import math

from ai.gateway.providers import ProviderEmbedding

# 一個 token 大約幾個字元（估算用，理由同 etl/tokens.py）。provider 沒回報用量時
# 寧可高估：低估會讓 2A 的成本統計看起來比實際便宜。
_CHARS_PER_TOKEN = 4


class MockEmbeddingProvider:
    """以 SHA-256 產生決定性的單位向量。"""

    name = "mock"

    def embed(self, texts: list[str], *, model: str, timeout_seconds: float) -> ProviderEmbedding:
        dimensions = _dimensions()
        return ProviderEmbedding(
            vectors=[_vector(text, dimensions) for text in texts],
            model=model,
            prompt_tokens=max(1, sum(len(text) for text in texts) // _CHARS_PER_TOKEN),
        )


def _dimensions() -> int:
    from config.settings.app_settings import get_app_settings

    return int(get_app_settings().ai_embedding_dimensions)


def _vector(text: str, dimensions: int) -> list[float]:
    """雜湊 → 單位向量。

    正規化成單位長度是為了讓 cosine 距離與內積一致——pgvector 的 HNSW 索引依 ops
    類別而異（05 §5.3），單位向量讓兩種算出來的排序相同，測試不必跟著索引設定改。
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    # 雜湊只有 32 位元組，維度通常是 1536：以計數器擴充，而不是重複同一段
    # ——重複的話向量會呈現週期性，任兩個向量的夾角會被那個週期綁住。
    raw: list[float] = []
    counter = 0
    while len(raw) < dimensions:
        block = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
        raw.extend(byte / 255.0 - 0.5 for byte in block)
        counter += 1

    values = raw[:dimensions]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]

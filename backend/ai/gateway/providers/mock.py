"""MockProvider —— 測試與本機開發的預設 provider（embedding 與 chat 各一個）。

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
import re
from collections.abc import AsyncGenerator

from ai.gateway.chat import (
    ChatRequest,
    ChatTimeouts,
    DoneDelta,
    ProviderDelta,
    TextDelta,
    UsageDelta,
)
from ai.gateway.providers import ProviderEmbedding, ProviderRerank, RerankedDocument

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


class MockChatProvider:
    """決定性的假串流（1D-3a）。

    性質與 `MockEmbeddingProvider` 要求的一樣，理由也一樣：**同樣的請求永遠得到同樣的
    字**。少了決定性，1D-4 的 SSE 測試只能斷言「有收到東西」，而那等於沒驗——事件順序、
    分段邊界、resume 之後接得上不接得上，全都需要一個對得起來的預期值。

    **它會把 context 裡的引用標記照抄回來**（`[c:...]`）。這不是為了好看：1D-5 的引用
    驗證要能走完「LLM 輸出標記 → 後處理比對本次 context → 組出 citations」整條路，而
    永遠不吐標記的 mock 會讓那條路的測試全部退化成「沒有引用也算通過」。它當然不理解
    問題——語意品質要等真模型與 Phase 2 的 golden set，這裡只驗機制。
    """

    name = "mock"

    async def stream_chat(
        self, request: ChatRequest, *, timeouts: ChatTimeouts
    ) -> AsyncGenerator[ProviderDelta, None]:
        answer = _answer(request)
        # 切成幾段吐出去。串流的呼叫端（1D-4 的 SSE、前端的逐字顯示）處理的是「一段
        # 一段到達」，一次吐完的 mock 驗不到那件事——而分段的邊界正是最容易寫錯的地方。
        for piece in _pieces(answer):
            yield TextDelta(text=piece)
        yield UsageDelta(
            prompt_tokens=_estimate("".join(message.content for message in request.messages)),
            completion_tokens=_estimate(answer),
            model=request.model,
            provider=self.name,
        )
        yield DoneDelta(finish_reason="stop")


# 06 §3.1 的引用標記格式。
_CITATION = re.compile(r"\[c:[^\]\s]+\]")
_PIECE_SIZE = 8


def _answer(request: ChatRequest) -> str:
    """問句 + context 裡的第一個引用標記 → 一段決定性的回答。"""
    question = next(
        (message.content for message in reversed(request.messages) if message.role == "user"),
        "",
    )
    markers = [
        marker
        for message in request.messages
        for marker in _CITATION.findall(message.content)
        # system 的模板本身可能含格式說明，那不是真的 context 標記。
        if message.role != "system"
    ]
    citation = f" {markers[0]}" if markers else ""
    return f"（mock）關於「{question.strip()}」，依據提供的內容回答如下。{citation}"


def _pieces(text: str) -> list[str]:
    return [text[index : index + _PIECE_SIZE] for index in range(0, len(text), _PIECE_SIZE)] or [""]


def _estimate(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


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


class MockRerankProvider:
    """假的 cross-encoder：**分數與查詢有關，但沒有語意**（2B-3）。

    與 `MockEmbeddingProvider` 同一個定位——它讓「rerank 有沒有被呼叫、有沒有真的改變
    順序、降級鏈會不會動」驗得出來，但**量不出品質**：分數是字元重疊比例，不是理解。
    真的品質評測要等 2B-4 接上 TEI 與 `bge-reranker-v2-m3`。

    分數落在 0~1，與真 cross-encoder 同一個尺度——否則 06 §3.1 的絕對門檻 0.3 會在假
    資料上通過、在真 provider 上失效。
    """

    name = "mock"

    def rerank(
        self, query: str, documents: list[str], *, model: str, timeout_seconds: float
    ) -> ProviderRerank:
        scored = [
            RerankedDocument(index=index, score=_overlap(query, document))
            for index, document in enumerate(documents)
        ]
        # 由高到低；同分時以索引為第二鍵（決定性，同 RRF 的理由）。
        scored.sort(key=lambda doc: (-doc.score, doc.index))
        return ProviderRerank(results=scored, model=model)


def _overlap(query: str, document: str) -> float:
    """查詢與文件的字元重疊比例（0~1）。完全相同 → 1.0，毫無交集 → 0.0。"""
    left, right = set(query.strip()), set(document.strip())
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)

"""`/rag/query` 的 I/O 契約（09 §2.3）。

輸出**明確列欄位**（同 knowledge 那份的理由）：從 `RetrievedChunk` 自動產生的話，
任何人在那個 dataclass 上加一個內部欄位都會自動流到 client，而不會有測試紅燈。
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, field_validator

from services.rag.retrieval import DEFAULT_TOP_K, MAX_TOP_K


class RagQueryIn(BaseModel):
    kb_id: uuid.UUID
    # ``min_length=1`` 擋空字串，validator 擋純空白——後者會被送去算 embedding，
    # 而 provider 對空字串各家行為不同，有的回一個沒有意義的向量，於是檢索出一組
    # 隨機的 chunk。那看起來像答案，實際上與問題無關。
    query: str = Field(min_length=1, max_length=4000)
    # 上限保護 DB：`top_k` 直接進 SQL 的 LIMIT，而呼叫端是外部整合方。沒有上限的話，
    # 一個極大值不會失敗，只會讓那台 DB 在那幾秒內對所有租戶都很慢。
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K)

    @field_validator("query")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("查詢不得為空白")
        return stripped


class RetrievedChunkOut(BaseModel):
    """一筆檢索命中。

    **欄位剛好足以組出一則引用**：哪個 chunk、出自哪份文件、第幾頁、哪一節。少了它們，
    這個端點就只是「回一堆文字」，而呼叫端無從得知那些文字是哪來的，也就無法驗證答案。

    ``score`` 是**相似度**（越大越相關，0~1），不是距離。
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    content: str
    score: float
    page: int | None
    heading_path: list[str]


class RagQueryOut(BaseModel):
    items: list[RetrievedChunkOut]

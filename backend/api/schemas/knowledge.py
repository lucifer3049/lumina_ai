"""`/knowledge-bases` 與 `/documents` 的 I/O 契約（09 §2.3）。

輸出型別**明確列欄位**，不是從 model 或 dataclass 自動產生：自動產生的話，任何人
在來源上加一個內部欄位（`storage_key`、`content_hash`）都會自動流到 client，而那
不會有任何測試紅燈。

列表一律包一層 ``items``，不回裸陣列——裸陣列之後要加分頁資訊（total、cursor）就是
破壞性變更，包一層之後加欄位是相容的（09 §1）。
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator


class KnowledgeBaseOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    status: str
    document_count: int


class KnowledgeBaseListOut(BaseModel):
    items: list[KnowledgeBaseOut]


def _reject_blank(value: str | None) -> str | None:
    """空白字元不算內容。

    ``min_length`` 擋得掉空字串，擋不掉 ``"   "``——那會建出一個在列表上看起來沒有
    名字的 KB，而使用者不知道要怎麼把它改回來（他看不到那是空白）。
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError("不得為空白")
    return stripped


class KnowledgeBaseCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        result = _reject_blank(value)
        assert result is not None
        return result


class KnowledgeBaseUpdateIn(BaseModel):
    """``None`` = 這次沒給（部分更新），不是「設為空」。"""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        return _reject_blank(value)


class DocumentOut(BaseModel):
    """**沒有 ``storage_key``**——理由見 `services/knowledge/documents.py` 的 DocumentView。"""

    id: uuid.UUID
    kb_id: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    status: str
    doc_version: int
    error: dict[str, Any] | None = None


class DocumentListOut(BaseModel):
    items: list[DocumentOut]

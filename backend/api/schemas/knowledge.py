"""`/knowledge-bases` 與 `/documents` 的 I/O 契約（09 §2.3）。

輸出型別**明確列欄位**，不是從 model 或 dataclass 自動產生：自動產生的話，任何人
在來源上加一個內部欄位（`storage_key`、`content_hash`）都會自動流到 client，而那
不會有任何測試紅燈。

列表一律包一層 ``items``，不回裸陣列——裸陣列之後要加分頁資訊（total、cursor）就是
破壞性變更，包一層之後加欄位是相容的（09 §1）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class KnowledgeBaseOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    status: str
    document_count: int
    # KB 級的參數覆寫（05 §3.2、15 §4.1 的第三層，2B-5）。**形狀刻意留成自由的
    # 物件**：逐鍵宣告成 pydantic 欄位的話，這裡會變成參數清單的第二份，而它與
    # `services/knowledge/kb_config.py` 的那一份漂掉時沒有任何測試會紅——症狀是
    # 新加的參數在 OpenAPI 上看不見，前端因此送不出去。驗證一律在寫入端做。
    config: dict[str, Any] = Field(default_factory=dict)
    # 「現有的 chunk 是不是用現在這組切塊參數切出來的」（2B-6）。**沒有這一欄的話**，
    # 使用者改完切塊參數看到的是一個「已儲存」的成功訊息，而既有 chunk 全部還是用
    # 舊參數切的——那個落差沒有任何地方顯示得出來。
    needs_reindex: bool = False


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
    # ``None`` = 沒給（建立時等同空的覆寫）。驗證在 Service
    # （`validate_kb_config`）——建立與更新因此走同一條，兩條各驗一次的話，其中
    # 一條遲早會漏掉新加的參數。
    config: dict[str, Any] | None = None

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
    # ``config`` 的「設為空」是明確的 ``{}``——那是使用者把一個調壞的 KB 還原的
    # 唯一出路，與「這次沒給」必須分得開。給了就是**整份取代**，不是逐鍵合併：
    # 深層合併讀起來比較體貼，但它讓「刪掉一個覆寫」變成不可能。
    config: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        return _reject_blank(value)


class KbReindexIn(BaseModel):
    """重建的請求（09 §2.3 的 ``POST /knowledge-bases/{id}/reindex``，2B-6）。

    兩個欄位都可省略——切塊參數改完之後按「重建」的人沒有要換模型，body 是 ``{}``。
    """

    # ``None`` = 沿用 KB 現行的 embedding 模型。
    target_model: str | None = Field(default=None, min_length=1, max_length=200)
    # ``None`` = 由 `knowledge_version` 判定（改過切塊參數就重切）。顯式傳入是給
    # 「chunker 本身改版」（不會動 knowledge_version）與「純換模型、不要順帶重切」
    # 這兩種情況用的。
    rechunk: bool | None = None

    @field_validator("target_model")
    @classmethod
    def _model_not_blank(cls, value: str | None) -> str | None:
        """``"   "`` 要在邊界就擋下。

        放進去的話它會照樣落地成一個永遠對不上的 ``(model, version)``（1C 的教訓），
        而症狀出現在幾十分鐘後——那時錯誤看起來與這次請求無關。
        """
        return _reject_blank(value)


class KbReindexJobOut(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID
    status: str
    target_model: str
    target_embedding_version: int
    rechunk: bool
    # 進度。前端要能區分「跑得慢」與「卡住」，只給 status 做不到。
    total_chunks: int
    embedded_chunks: int
    total_documents: int
    rechunked_documents: int
    started_at: datetime | None = None
    # 切換完成的時刻——也是可回退觀察期的起點（06 §2.2 第 4 步）。
    switched_at: datetime | None = None
    finished_at: datetime | None = None
    error: dict[str, Any] | None = None


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

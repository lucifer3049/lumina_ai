"""`/conversations` 的 I/O 契約（09 §1.1、§2.4）。

輸出**明確列欄位**（同 knowledge、rag 的理由）：從 dataclass 自動產生的話，任何人在
來源上加一個內部欄位都會自動流到 client，而不會有測試紅燈。

分頁的回應形狀是 `{items, next_cursor}`（09 §1.1）。**`next_cursor` 為 null 代表沒有
下一頁**——永遠給一個游標的話，前端的無限捲動會一直往下打而每次拿到空清單。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from services.conversation.conversations import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

# ── 輸入的硬上限 ────────────────────────────────────────────────
#
# 形狀同 `services/rag/params.py` 的 `MAX_TOP_K`：**保護我們自己的上限，不是使用者
# 可調的偏好**，因此住在程式碼裡而不是設定檔（15 §4.1 的例外條款）。可調的那一半是
# quota（tokens_month／messages_day），那才是「這個租戶可以用多少」。

# 一則訊息的字元上限。同一張 schema 的 `title` 有 200、`prompt_key` 有 100，而**內容
# 本身原本什麼都沒有**——最主要的那條路徑反而沒設防（`/rag/query` 的 query 早就有
# 4000）。
#
# 沒有上限時，一則 5MB 的訊息會沿著三條路各放大一次：存進 `conversation_message`、
# 進歷史視窗（**之後每一輪都重送一次**）、直送 provider 按 prompt token 計價。而開場
# 的 quota 預留是一個固定估計值，真正的用量要等生成結束才追認——擋線在那之前形同虛設。
#
# 32,000 取的是「與 context 預算同一個量級」：中文約 1 字 1 token，也就是三萬多
# token，已經比任何一輪的 context 預算（06 §3.2 的 4,500）大一個量級；再大就不是
# 「一個問題」，而是應該走文件上傳的東西。
MAX_MESSAGE_CHARS = 32_000

# 一場對話能掛幾個 KB。**KB 數量本身不在 quota 資源清單裡**（04 §8.1 只有 documents／
# storage_bytes／tokens／messages／streams），所以租戶建幾千個 KB 全掛上同一場對話是
# 做得到的——而每一則訊息都會為每個 KB 各開一次交易查設定、各跑一次檢索。
# 上限先擋在這裡；把 KB 數量列入 quota 是另一件事（記入 backlog）。
MAX_CONVERSATION_KBS = 10


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    kb_ids: list[uuid.UUID]
    prompt_key: str
    status: str
    pinned: bool
    message_count: int
    last_message_at: datetime | None


class ConversationListOut(BaseModel):
    items: list[ConversationOut]
    next_cursor: str | None


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    # 1E 的引用面板靠它渲染；jsonb 原樣回傳（05 §3.4）。
    citations: list[dict[str, Any]]
    model: str
    status: str
    usage: dict[str, Any]
    created_at: datetime


class MessageListOut(BaseModel):
    items: list[MessageOut]
    next_cursor: str | None


class MessageCreateIn(BaseModel):
    """送出一個問題（1D-4a）。"""

    content: str = Field(max_length=MAX_MESSAGE_CHARS)


class TurnStartedOut(BaseModel):
    """建立回合的回應（09 §2.4，1D-4a 拆成兩步後的第一步）。

    **client 在收到任何一個位元組之前就拿到 `message_id`。** 它是後續三件事唯一的
    定位鍵：讀串流、按停止（1D-4b）、以及斷線後直接抓最終訊息。只靠 `meta` 事件的話，
    生成失敗時那個事件永遠不會來，而 client 手上沒有任何東西可以查。
    """

    message_id: uuid.UUID
    user_message_id: uuid.UUID
    conversation_id: uuid.UUID
    # 串流的位址由伺服器給，client 不自己拼——網址形狀之後要改時，改一邊就好。
    stream_url: str


def _reject_blank(value: str | None) -> str | None:
    """空白字元不算內容（同 knowledge 的理由）。"""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError("不得為空白")
    return stripped


class ConversationCreateIn(BaseModel):
    # 標題可以是空的——新對話通常由第一則訊息自動命名（1D-4）。
    title: str = Field(default="", max_length=200)
    kb_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_CONVERSATION_KBS)
    prompt_key: str = Field(default="", max_length=100)


class ConversationUpdateIn(BaseModel):
    """部分更新（09 §2.4：改名／釘選／封存）。

    **全部 optional**：`None` 代表「client 沒給」，由 Service 濾掉。當成「設為空」的話，
    使用者改一次標題就會把釘選狀態清掉。
    """

    title: str | None = Field(default=None, max_length=200)
    pinned: bool | None = None
    status: str | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str | None) -> str | None:
        return _reject_blank(value)

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str | None) -> str | None:
        if value is not None and value not in {"active", "archived"}:
            raise ValueError("status 只能是 active 或 archived")
        return value


class PageParams(BaseModel):
    """分頁查詢參數（09 §1.1：預設 20、上限 100）。

    上限是保護 DB 的：`limit` 直接進 SQL，而沒有上限時一個極大值不會失敗，只會讓
    那幾秒對所有租戶都很慢。
    """

    cursor: str | None = None
    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)

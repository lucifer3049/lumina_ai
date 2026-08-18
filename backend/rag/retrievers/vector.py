"""向量檢索的**純邏輯**（06 §3.1、02 §2 的 `rag/retrievers/vector.py`）。

這一層刻意很薄，而它薄的理由與 `etl/` 相同（鐵則 2）：**`rag/` 不碰 ORM、不認識上層**。
真正的 SQL 在 `EmbeddingRepository.search`，由 service 呼叫後把結果餵進來；這裡只做那些
「換了資料來源也不會變」的事——正規化查詢字串、把命中整理成下游要的形狀、套用門檻。

現在只有一條路（純向量），所以看起來像多此一舉。它存在是因為 Phase 2 的 hybrid 要在
**同一個位置**把兩路候選以 RRF 融合，而融合是純函式——那時若 `rag/` 不存在，這段邏輯
只能塞進 service 或 repository，前者會讓 service 變成演算法的家，後者會讓它綁死 SQL。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """檢索結果的一筆——**上層只認得這個型別**。

    `page` 與 `heading_path` 從 chunk 的 meta 攤平出來：1D 的引用要靠它們說出「這句話
    出自哪份文件第幾頁」，而 meta 是一個什麼都能塞的 dict，讓每個呼叫端各自去挖等於
    讓每個呼叫端各自寫一次「如果沒有這個鍵怎麼辦」。

    `document_name` 與 `doc_version` 是 1D-5 加的，兩個都只為了引用（06 §3.3）：
    面板要說得出「出自《人事規章.pdf》第 7 頁」，而只有一串 UUID 說不出來；版本則讓
    文件重新上傳之後，舊回答仍指得出當時引用的是哪一版。**兩筆資料在 SQL 那一趟就
    一起撈回來**——事後補查等於每則回答多 N 次查詢，而那是引用面板最頻繁的路徑。
    """

    chunk_id: UUID
    document_id: UUID
    content: str
    score: float
    page: int | None
    heading_path: list[str]
    # 有預設值：`rag/` 的既有呼叫端（與測試）不必為了兩個引用用的欄位全部改一遍。
    document_name: str = ""
    doc_version: int = 1


class VectorHitLike(Protocol):
    """repository 回傳的命中形狀（結構比對，不 import repositories——鐵則 2）。

    **屬性宣告成 read-only property 而不是欄位**：Protocol 的可變欄位要求實作端的屬性
    也是可寫的，而 repository 回傳的是 frozen dataclass（不可變是刻意的——檢索結果會
    一路傳到 prompt，中途被改掉會讓引用對不回來）。寫成欄位的話，mypy 會拒絕那個
    frozen 型別，而唯一的「修法」是把不可變性拿掉。
    """

    @property
    def chunk_id(self) -> UUID: ...
    @property
    def document_id(self) -> UUID: ...
    @property
    def document_name(self) -> str: ...
    @property
    def doc_version(self) -> int: ...
    @property
    def content(self) -> str: ...
    @property
    def meta(self) -> dict[str, Any]: ...
    @property
    def score(self) -> float: ...


def normalise_query(query: str) -> str:
    """查詢前處理（06 §3 的「Query 前處理」）。

    目前只有 strip。**空白查詢在這裡變成空字串**，由呼叫端拒絕——不能讓它往下走：
    provider 對空字串的行為各家不同，有的回錯、有的回一個沒有意義的向量，而後者會
    檢索出一組隨機的 chunk。那看起來像答案，實際上與問題無關。

    condense（多輪改寫成獨立問句）屬 1D：它需要對話歷史，而這裡只看得到一句話。
    """
    return query.strip()


def to_retrieved(hits: Sequence[VectorHitLike]) -> list[RetrievedChunk]:
    """repository 的命中 → 下游的形狀。

    **不重新排序**：順序由 SQL 的 ``ORDER BY distance`` 決定，那是索引給的順序。在這裡
    再排一次的話，兩邊的排序規則哪天不一致時，症狀是「分數明明比較高卻排後面」，而沒有
    任何一邊看起來是錯的。
    """
    return [
        RetrievedChunk(
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            document_name=hit.document_name,
            doc_version=hit.doc_version,
            content=hit.content,
            score=hit.score,
            page=_page(hit.meta),
            heading_path=_heading_path(hit.meta),
        )
        for hit in hits
    ]


def _page(meta: dict[str, Any]) -> int | None:
    """頁碼可以不存在（Markdown、xlsx 這類沒有頁的來源），但不能是別的型別。"""
    value = meta.get("page")
    return int(value) if isinstance(value, int) else None


def _heading_path(meta: dict[str, Any]) -> list[str]:
    value = meta.get("heading_path")
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]

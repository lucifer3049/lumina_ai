"""Excel (.xlsx) loader（08 §3：每 sheet 一節、表頭隨列窗重複）。

**試算表沒有敘事。** 它沒有段落、沒有頁，只有一大片格子。結構因此只靠兩件事撐住：

- **工作表名**就是這一節的標題（成為底下每一塊的 ``heading_path``）；
- **表頭**決定每一欄的意義。

大表按列窗切成多個 TABLE block，**每一塊都重複帶上表頭**。表頭只放第一塊的話，
後面每一塊的每一列都會失去欄位歸屬——而檢索命中的往往正是後面那些塊，它們才是資料。

切窗發生在 loader 而不是 chunker，是 08 §3 對這個來源的指名做法：一張十萬列的表若
以單一 block 交給下游，1B-5 的「表格不拆散」會讓它整塊變成一個超出任何 context
window 的 chunk——那條規則對文件內的小表成立，對試算表不成立。

`read_only=True`：openpyxl 預設把整份活頁簿建成物件圖，十萬列的檔案會吃掉數 GB。
`data_only=True`：取公式的**快取值**而不是公式字串——使用者要問的是數字，而
「=SUM(B2:B99)」對檢索毫無意義。代價是從未被 Excel 開啟過的檔案沒有快取值（見 `_cell`）。
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import date, datetime, time
from typing import Any

import openpyxl

from core.exceptions import ExtractionFailedError
from etl.extract.loaders.base import HeadingStack, LoadResult
from etl.extract.markdown import rows_to_markdown_table
from etl.extract.model import Block, BlockMeta, BlockType

# 一個 TABLE block 最多幾列資料（不含重複附帶的表頭）。50 列大致對應 1B-5 的單一
# chunk 預算：太大則超出 context window、太小則表頭的重複成本佔比失控。
ROWS_PER_BLOCK = 50


def load(content: bytes) -> LoadResult:
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise ExtractionFailedError("xlsx 開啟失敗", cause=type(exc).__name__) from exc

    blocks: list[Block] = []
    headings = HeadingStack()
    # **在 close() 之前取值**：read_only 模式下 `close()` 只關檔案柄，`worksheets`
    # 目前仍讀得到——但那是 openpyxl 的內部細節，不是它承諾的東西。把「關掉之後還能
    # 讀什麼」押在一個實作細節上，代價是升版之後這裡會丟一個與 xlsx 內容無關的例外，
    # 而症狀是「某些試算表突然解析失敗」。
    sheet_count = len(workbook.worksheets)
    try:
        for sheet in workbook.worksheets:
            rows = [row for row in _rows(sheet) if any(cell for cell in row)]
            if not rows:
                # 完全空的工作表是活頁簿的殘留，不是內容。
                continue

            headings.push(1, str(sheet.title))
            blocks.append(
                Block(
                    type=BlockType.HEADING,
                    text=str(sheet.title),
                    # 試算表沒有頁的概念——page 留 None（同純文字的理由）。
                    meta=BlockMeta(order=len(blocks), heading_path=headings.path()[:-1]),
                )
            )

            header, *data = rows
            for window in _windows(data, ROWS_PER_BLOCK):
                blocks.append(
                    Block(
                        type=BlockType.TABLE,
                        text=rows_to_markdown_table([header, *window]),
                        meta=BlockMeta(order=len(blocks), heading_path=headings.path()),
                    )
                )
    except Exception as exc:
        raise ExtractionFailedError("xlsx 解析失敗", cause=type(exc).__name__) from exc
    finally:
        workbook.close()

    doc_meta: dict[str, Any] = {
        "sheet_count": sheet_count,
        "table_count": sum(1 for block in blocks if block.type is BlockType.TABLE),
    }
    return blocks, doc_meta


def _rows(sheet: Any) -> Iterator[list[str]]:
    for row in sheet.iter_rows(values_only=True):
        yield [_cell(value) for value in row]


def _cell(value: object) -> str:
    """格子 → 字串。

    日期時間走 ISO 8601 而不是 ``str()``：後者對 ``datetime`` 會給出帶空格的格式，
    而同一份表裡不同格子的顯示格式可能不同——檢索與比對需要一種穩定的寫法。

    ``None`` 有兩個來源：真的空格子，以及 ``data_only=True`` 之下**從未被 Excel
    計算過**的公式格。兩者都寫成空字串：把「未計算」表達成任何其他東西（公式字串、
    佔位符）都會被當成資料嵌入。
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date | time):
        return value.isoformat()
    return str(value).strip()


def _windows(rows: list[list[str]], size: int) -> Iterator[list[list[str]]]:
    """把資料列切成固定大小的窗。沒有資料列時仍產出一個空窗——只有表頭的表也要留住。"""
    if not rows:
        yield []
        return
    for start in range(0, len(rows), size):
        yield rows[start : start + size]

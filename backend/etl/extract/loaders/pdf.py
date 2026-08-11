"""PDF loader（08 §3：pymupdf 文字層）。

**PDF 沒有語意標記，只有視覺屬性**——標題偵測必然是啟發式的。這裡用字級：比內文
明顯大的行視為標題。選字級而不是粗體或位置，是因為它在三者中最少誤判（粗體常用於
強調；位置對雙欄排版完全失效）。判錯的後果是 heading_path 不準（引用指到相鄰的
節），不是壞掉——所以這個啟發式可以接受，而 docx 那種**樣式**判定的可信度更高，
1C 評估檢索品質時要記得兩者不同。

**掃描件（沒有文字層）在這裡會失敗**而不是回一份空文件：08 §3 的 OCR fallback 預設
關閉且不在 1B 範圍。回空的話，文件會走完整條 ETL 停在 ready 而一個 chunk 都沒有，
使用者問問題時只會覺得「這份文件好像沒被讀進去」，沒有任何地方看得出原因。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pymupdf

from core.exceptions import ExtractionFailedError
from etl.extract.loaders.base import HeadingStack, LoadResult
from etl.extract.model import Block, BlockMeta, BlockType

# 大於內文字級這個倍數的行視為標題。1.2 是排版慣例裡「看得出是標題」的下界；
# 訂得更低會把強調用的稍大字體誤判成標題，更高則接不住只大一點的小節標題。
_HEADING_SIZE_RATIO = 1.2

# 字級的比較先四捨五入：同一段文字的 span 字級常有 0.01 級的浮點差異，
# 不收斂的話「內文字級」會被切成好幾個幾乎相同的值。
_SIZE_PRECISION = 1


@dataclass(frozen=True, slots=True)
class _Line:
    """頁面上的一行：文字、字級，以及它屬於哪一頁的哪個 pymupdf block。"""

    page: int
    block_index: int
    text: str
    size: float


def load(content: bytes) -> LoadResult:
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise ExtractionFailedError(
            "PDF 開啟失敗",
            cause=type(exc).__name__,
        ) from exc

    try:
        lines = _read_lines(document)
        page_count = document.page_count
    finally:
        document.close()

    if not lines:
        raise ExtractionFailedError(
            "PDF 沒有可抽取的文字層（掃描件需 OCR，目前未啟用）",
            cause="empty_document",
            details={"page_count": page_count},
        )

    blocks = _build_blocks(lines)
    doc_meta: dict[str, Any] = {"page_count": page_count}
    return blocks, doc_meta


def _read_lines(document: pymupdf.Document) -> list[_Line]:
    """把每一頁攤平成「行」。

    以**行**而不是 pymupdf 的 block 為單位判定標題：pymupdf 依版面鄰近性分塊，
    標題與緊接其下的內文常常落在同一個 block 裡。以 block 為單位的話，那個 block
    只會有一個字級（取最大值即整段變標題，取平均則標題消失）。
    """
    lines: list[_Line] = []
    for page_index in range(document.page_count):
        page = document.load_page(page_index)
        content = page.get_text("dict")
        for block_index, block in enumerate(content.get("blocks", [])):
            # type 1 是圖片；圖片的內容抽取（OCR、caption）不在 1B 範圍。
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = "".join(span.get("text", "") for span in spans).strip()
                if not text:
                    continue
                size = max((float(span.get("size", 0.0)) for span in spans), default=0.0)
                lines.append(
                    _Line(
                        page=page_index + 1,  # 頁碼從 1 開始——那是使用者看到的編號
                        block_index=block_index,
                        text=text,
                        size=round(size, _SIZE_PRECISION),
                    )
                )
    return lines


def _body_size(lines: list[_Line]) -> float:
    """內文字級 = 佔字數最多的那個字級。

    用**字數**加權而不是行數：一份文件裡標題的行數可能不少（每節一條），但字數
    永遠遠少於內文。以行數投票時，短文件的標題可能反而勝出，於是內文全被判成標題。
    """
    weight: dict[float, int] = {}
    for line in lines:
        weight[line.size] = weight.get(line.size, 0) + len(line.text)
    # 同票時取較小的字級：內文比標題小，猜錯方向的代價（整份文件變標題）比較大。
    return max(weight.items(), key=lambda item: (item[1], -item[0]))[0]


def _heading_levels(lines: list[_Line], body_size: float) -> dict[float, int]:
    """標題字級 → 層級（字級愈大層級愈外層，從 1 起算）。"""
    threshold = body_size * _HEADING_SIZE_RATIO
    sizes = sorted({line.size for line in lines if line.size >= threshold}, reverse=True)
    return {size: level for level, size in enumerate(sizes, start=1)}


def _build_blocks(lines: list[_Line]) -> list[Block]:
    body_size = _body_size(lines)
    levels = _heading_levels(lines, body_size)

    blocks: list[Block] = []
    headings = HeadingStack()
    # 累積中的段落：同一個 pymupdf block 內的連續內文行屬於同一段
    # （那正是「一段話被視窗寬度折行」的形狀）。
    buffer: list[str] = []
    buffer_key: tuple[int, int] | None = None

    def flush() -> None:
        nonlocal buffer, buffer_key
        if not buffer or buffer_key is None:
            return
        blocks.append(
            Block(
                type=BlockType.PARAGRAPH,
                text="\n".join(buffer),
                meta=BlockMeta(
                    order=len(blocks),
                    page=buffer_key[0],
                    heading_path=headings.path(),
                ),
            )
        )
        buffer = []
        buffer_key = None

    for line in lines:
        level = levels.get(line.size)
        if level is not None:
            flush()
            headings.push(level, line.text)
            blocks.append(
                Block(
                    type=BlockType.HEADING,
                    text=line.text,
                    # 標題自己的 heading_path 是它的**祖先**，不含自己——
                    # 含自己的話，引用顯示會變成「〈第一章〉的〈第一章〉」。
                    meta=BlockMeta(
                        order=len(blocks),
                        page=line.page,
                        heading_path=headings.path()[:-1],
                    ),
                )
            )
            continue

        key = (line.page, line.block_index)
        if buffer_key is not None and key != buffer_key:
            flush()
        buffer_key = key
        buffer.append(line.text)

    flush()
    return blocks

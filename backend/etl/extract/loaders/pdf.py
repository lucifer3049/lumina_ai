"""PDF loader（08 §3，1B-4c 起以 pdfplumber 實作）。

**標題有兩個來源，優先順序固定：**

1. **PDF 大綱（書籤）**——作者明確標記的階層。有大綱時再去猜字級，等於把已知的正確
   答案丟掉換一個估計值，而那個估計值在字級一致的文件上必然失敗。
2. **字級啟發式**——沒有大綱時的退路。PDF 本身沒有語意標記，只有視覺屬性；選字級而
   非粗體或位置，是因為它最少誤判（粗體常用於強調，位置對雙欄排版完全失效）。判錯的
   後果是 heading_path 不準（引用指到相鄰的節），不是壞掉。

**表格獨立成 block，且其文字不再以段落形式重複出現。** 重複的後果不是多一份而已：
同一份內容會被切成兩種形狀各自嵌入，檢索時互相排擠，而回答引用到的往往是那個沒有
欄位結構的版本。

**掃描件（沒有文字層）在這裡失敗**而不是回一份空文件：OCR 預設關閉且不在 1B 範圍。
回空的話文件會走完整條 ETL 停在 ready 而一個 chunk 都沒有，使用者問問題時只會覺得
「這份文件好像沒被讀進去」，沒有任何地方看得出原因。

選 pdfplumber 而非 PyMuPDF：後者是 AGPL-3.0，其網路使用條款對多租戶 SaaS 會實際
觸發（1B-4c 決策）。pdfplumber（MIT，底層 pdfminer.six）逐字元給得出字級，表格偵測
內建，換過去不損失本 loader 需要的任何資訊；代價是純 Python、大檔較慢，而 ETL 的
SLO 是分鐘級，且抽取本來就跑在有逾時的子行程裡（見 `etl.extract.sandbox`）。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import pdfplumber

from core.exceptions import ExtractionFailedError
from etl.extract.loaders.base import HeadingStack, LoadResult
from etl.extract.markdown import rows_to_markdown_table
from etl.extract.model import Block, BlockMeta, BlockType

# 大於內文字級這個倍數的行視為標題。1.2 是排版慣例裡「看得出是標題」的下界；
# 更低會把強調用的稍大字體誤判成標題，更高則接不住只大一點的小節標題。
_HEADING_SIZE_RATIO = 1.2

# 字級比較前先四捨五入：同一段文字的字級常有 0.01 級的浮點差異，不收斂的話
# 「內文字級」會被切成好幾個幾乎相同的值。
_SIZE_PRECISION = 1

# 兩行之間的垂直間距超過行高的這個倍數時視為分段。1.6 容得下一般行距，又擋得住
# 段落之間刻意留出的空白。判太鬆會把整頁併成一塊，判太嚴則每行自成一塊。
_PARAGRAPH_GAP_RATIO = 1.6


@dataclass(frozen=True, slots=True)
class _Line:
    """頁面上的一行：文字、字級與垂直位置。"""

    page: int
    text: str
    size: float
    top: float
    height: float


@dataclass(frozen=True, slots=True)
class _Table:
    """一張表格：已轉成 GFM 的文字與它在頁面上的垂直位置。"""

    page: int
    text: str
    top: float


def load(content: bytes) -> LoadResult:
    try:
        pdf = pdfplumber.open(io.BytesIO(content))
    except Exception as exc:
        raise ExtractionFailedError("PDF 開啟失敗", cause=type(exc).__name__) from exc

    try:
        outline = _outline_levels(pdf)
        page_count = len(pdf.pages)
        lines: list[_Line] = []
        tables: list[_Table] = []
        for page_number, page in enumerate(pdf.pages, start=1):
            page_tables, boxes = _read_tables(page, page_number)
            tables.extend(page_tables)
            lines.extend(_read_lines(page, page_number, boxes))
    except Exception as exc:
        raise ExtractionFailedError("PDF 解析失敗", cause=type(exc).__name__) from exc
    finally:
        pdf.close()

    if not lines and not tables:
        raise ExtractionFailedError(
            "PDF 沒有可抽取的文字層（掃描件需 OCR，目前未啟用）",
            cause="empty_document",
            details={"page_count": page_count},
        )

    blocks = _build_blocks(lines, tables, outline)
    doc_meta: dict[str, Any] = {
        "page_count": page_count,
        "table_count": len(tables),
        # 標題來源進 doc_meta：大綱是作者標的、字級是我們猜的，兩者的可信度不同。
        # 1C 評估檢索品質時，「這份文件的階層是猜的」是必要的背景資訊。
        "heading_source": "outline" if outline else "font_size",
    }
    return blocks, doc_meta


def _outline_levels(pdf: Any) -> dict[str, int]:
    """PDF 大綱 → ``{標題文字: 層級}``。沒有大綱或讀不出來時回空 dict。

    以**標題文字**而不是目的地座標對應回內文：座標要解析 destination（間接參照、
    具名目的地、各家產生器的方言），而那條路徑上每一種畸形寫法都會讓整份大綱失效。
    文字比對的失敗方式溫和得多——對不上就退回字級啟發式。

    大綱讀取失敗**不算錯誤**：它是加分項，不是必要資訊。
    """
    levels: dict[str, int] = {}
    try:
        entries = pdf.doc.get_outlines()
    except Exception:
        # pdfminer 對沒有大綱的檔案丟 PDFNoOutlines，畸形大綱則丟各種解析例外。
        return levels

    try:
        for level, title, *_ in entries:
            normalized = _normalize(str(title))
            if normalized and normalized not in levels:
                levels[normalized] = int(level)
    except Exception:
        return {}
    return levels


def _read_tables(page: Any, page_number: int) -> tuple[list[_Table], list[tuple[float, ...]]]:
    """抽出頁面上的表格，並回傳它們的邊界框供文字抽取排除。"""
    tables: list[_Table] = []
    boxes: list[tuple[float, ...]] = []
    for table in page.find_tables():
        rows = [[(cell or "") for cell in row] for row in table.extract()]
        rows = [row for row in rows if any(cell.strip() for cell in row)]
        if not rows:
            continue
        boxes.append(tuple(table.bbox))
        tables.append(
            _Table(
                page=page_number,
                # GFM 在 loader 產生：只有這裡看得到儲存格的行列邊界（見 markdown 模組）。
                text=rows_to_markdown_table(rows),
                top=float(table.bbox[1]),
            )
        )
    return tables, boxes


def _read_lines(page: Any, page_number: int, table_boxes: list[tuple[float, ...]]) -> list[_Line]:
    """頁面的文字行，**排除落在表格邊界內的字元**。"""
    source = page.filter(lambda obj: not _inside_any(obj, table_boxes)) if table_boxes else page

    lines: list[_Line] = []
    for line in source.extract_text_lines():
        text = str(line["text"]).strip()
        if not text:
            continue
        sizes = [float(char["size"]) for char in line["chars"]]
        lines.append(
            _Line(
                page=page_number,
                text=text,
                size=round(max(sizes), _SIZE_PRECISION) if sizes else 0.0,
                top=float(line["top"]),
                height=float(line["bottom"]) - float(line["top"]),
            )
        )
    return lines


def _inside_any(obj: dict[str, Any], boxes: list[tuple[float, ...]]) -> bool:
    """物件的中心點是否落在任一表格框內。

    用中心點而不是完全包含：儲存格的框線與文字常有一兩點的重疊，要求完全包含時，
    貼著邊界的那些字會漏網並以段落形式重複出現一次。
    """
    x = (float(obj.get("x0", 0.0)) + float(obj.get("x1", 0.0))) / 2
    y = (float(obj.get("top", 0.0)) + float(obj.get("bottom", 0.0))) / 2
    return any(x0 <= x <= x1 and top <= y <= bottom for x0, top, x1, bottom in boxes)


def _normalize(text: str) -> str:
    """比對用的正規化：去頭尾空白並壓縮內部空白。

    大綱的標題與頁面上的文字常有空白差異（PDF 的字距由座標決定，抽取時可能多一個
    空格）。不正規化的話，比對會在「看起來一模一樣」的兩個字串上失敗。
    """
    return " ".join(text.split())


def _body_size(lines: list[_Line]) -> float:
    """內文字級 = 佔字數最多的那個字級。

    用**字數**加權而不是行數：標題的行數可能不少（每節一條），但字數永遠遠少於
    內文。以行數投票時，短文件的標題可能反而勝出，於是內文全被判成標題。
    """
    weight: dict[float, int] = {}
    for line in lines:
        weight[line.size] = weight.get(line.size, 0) + len(line.text)
    if not weight:
        return 0.0
    # 同票時取較小的字級：內文比標題小，猜錯方向的代價（整份文件變標題）比較大。
    return max(weight.items(), key=lambda item: (item[1], -item[0]))[0]


def _heading_levels_by_size(lines: list[_Line]) -> dict[float, int]:
    """標題字級 → 層級（字級愈大層級愈外層，從 1 起算）。"""
    body = _body_size(lines)
    threshold = body * _HEADING_SIZE_RATIO
    sizes = sorted({line.size for line in lines if line.size >= threshold}, reverse=True)
    return {size: level for level, size in enumerate(sizes, start=1)}


def _build_blocks(
    lines: list[_Line],
    tables: list[_Table],
    outline: dict[str, int],
) -> list[Block]:
    size_levels = {} if outline else _heading_levels_by_size(lines)

    blocks: list[Block] = []
    headings = HeadingStack()
    buffer: list[_Line] = []

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        blocks.append(
            Block(
                type=BlockType.PARAGRAPH,
                text="\n".join(line.text for line in buffer),
                meta=BlockMeta(
                    order=len(blocks),
                    page=buffer[0].page,
                    heading_path=headings.path(),
                ),
            )
        )
        buffer = []

    for item in _in_reading_order(lines, tables):
        if isinstance(item, _Table):
            flush()
            blocks.append(
                Block(
                    type=BlockType.TABLE,
                    text=item.text,
                    meta=BlockMeta(order=len(blocks), page=item.page, heading_path=headings.path()),
                )
            )
            continue

        level = outline.get(_normalize(item.text)) or size_levels.get(item.size)
        if level is not None:
            flush()
            headings.push(level, item.text)
            blocks.append(
                Block(
                    type=BlockType.HEADING,
                    text=item.text,
                    # 標題自己的 heading_path 是它的**祖先**，不含自己——含自己的話，
                    # 引用會顯示成「〈第一章〉的〈第一章〉」。
                    meta=BlockMeta(
                        order=len(blocks),
                        page=item.page,
                        heading_path=headings.path()[:-1],
                    ),
                )
            )
            continue

        if buffer and _starts_new_paragraph(buffer[-1], item):
            flush()
        buffer.append(item)

    flush()
    return blocks


def _in_reading_order(lines: list[_Line], tables: list[_Table]) -> list[_Line | _Table]:
    """把行與表格依「第幾頁、頁上多高」排成閱讀順序。

    單欄排版下這就是正確順序；雙欄會交錯（左右欄的 y 值重疊）——那是已知限制，
    要正確處理需要版面分欄偵測，屬 Phase 2 依評測數據決定的範圍。
    """
    items: list[_Line | _Table] = [*lines, *tables]
    return sorted(items, key=lambda item: (item.page, item.top))


def _starts_new_paragraph(previous: _Line, current: _Line) -> bool:
    """跨頁、字級改變、或垂直間距過大時另起一段。

    跨頁必分：頁尾與次頁頁首的 y 座標沒有可比性，不分的話兩段永遠會被黏在一起。
    """
    if current.page != previous.page:
        return True
    if current.size != previous.size:
        return True
    line_height = max(previous.height, current.height, 1.0)
    return (current.top - previous.top) > line_height * _PARAGRAPH_GAP_RATIO

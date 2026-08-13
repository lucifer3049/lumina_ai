"""Markdown loader（1B-4b）。

Markdown 是**唯一結構已經明說的文字來源**：標題就是標題、表格就是表格，不必像 PDF
那樣從字級去猜。因此這個 loader 的錯誤方式只有一種——把已經標好的結構弄丟。

**block 的文字直接取自原始碼片段**（除了標題要去掉井字號）。理由有兩層：一是我們
下游餵給 LLM 的本來就是 Markdown（見 `etl.extract.markdown`），重新產生一份等於在
來源與輸出之間插入一次無謂的往返；二是清單、巢狀引用這類結構若逐項拆解再組回去，
縮排與標記幾乎一定會走樣，而那種走樣沒有任何測試會抓到。

用 markdown-it-py（MIT）而不是自己切行：setext 標題、fence 內的井字號、表格對齊列、
巢狀清單——每一項都是「看起來簡單，實際上是解析器」的陷阱。走 CommonMark 規格再
啟用 table 擴充，與我們自己輸出的 GFM 表格對得起來。

（本模組與 `etl.extract.markdown`（渲染器）同名不同套件：一個把 Markdown 讀進來，
另一個把 blocks 寫出去。Python 的 import 是絕對的，兩者不衝突。）
"""

from __future__ import annotations

from typing import Any

from markdown_it import MarkdownIt

from core.exceptions import ExtractionFailedError
from etl.extract.loaders.base import HeadingStack, LoadResult
from etl.extract.model import Block, BlockMeta, BlockType

# token.type → block 型別。**沒有列在這裡的頂層 token 一律當段落**（引用、清單、
# HTML 區塊…）：它們的共同點是「一段有結構的文字」，而把它們原樣保留，
# 遠比為每一種各寫一條規則要少出錯。
_BLOCK_TYPES = {
    "heading_open": BlockType.HEADING,
    "table_open": BlockType.TABLE,
    "fence": BlockType.CODE,
    "code_block": BlockType.CODE,
}

# 不產生 block 的頂層 token：分隔線沒有內容，front matter 是給產生器看的中繼資料。
_IGNORED = {"hr", "front_matter"}

_PARSER = MarkdownIt("commonmark").enable("table")


def load(content: bytes) -> LoadResult:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractionFailedError(
            "Markdown 解碼失敗（非 UTF-8）",
            cause=type(exc).__name__,
        ) from exc

    try:
        tokens = _PARSER.parse(text)
    except Exception as exc:
        raise ExtractionFailedError("Markdown 解析失敗", cause=type(exc).__name__) from exc

    lines = text.split("\n")
    blocks: list[Block] = []
    headings = HeadingStack()

    for token in tokens:
        # 只看頂層（nesting level 0）的開啟 token：巢狀內容由來源片段整段帶走。
        if token.level != 0 or token.type.endswith("_close") or token.type in _IGNORED:
            continue

        block_type = _BLOCK_TYPES.get(token.type, BlockType.PARAGRAPH)
        body = _source(token, lines)
        if not body:
            continue

        if block_type is BlockType.CODE:
            # fence 的 token.content 已經是去掉圍欄的程式碼；渲染器會自己加回圍欄，
            # 沿用來源片段會變成雙層圍欄。
            body = str(token.content).rstrip("\n")

        if block_type is BlockType.HEADING:
            title = _heading_text(body)
            level = int(token.tag[1:]) if token.tag[1:].isdigit() else 1
            headings.push(level, title)
            blocks.append(
                Block(
                    type=BlockType.HEADING,
                    text=title,
                    # Markdown 沒有頁的概念（同純文字）。
                    meta=BlockMeta(order=len(blocks), heading_path=headings.path()[:-1]),
                )
            )
            continue

        blocks.append(
            Block(
                type=block_type,
                text=body,
                meta=BlockMeta(order=len(blocks), heading_path=headings.path()),
            )
        )

    doc_meta: dict[str, Any] = {
        "table_count": sum(1 for block in blocks if block.type is BlockType.TABLE),
    }
    return blocks, doc_meta


def _source(token: Any, lines: list[str]) -> str:
    """token 對應的原始碼片段。``map`` 是 [起始行, 結束行)，結束行不含。"""
    if token.map is None:
        return str(token.content).strip()
    start, end = token.map
    return "\n".join(lines[start:end]).strip()


def _heading_text(source: str) -> str:
    """去掉標題的標記，留下文字。

    ATX 標題可能寫成 ``## 標題 ##``（結尾的井字號是裝飾），setext 標題則是文字加
    底線那一行——兩種都要還原成同一個東西，因為 heading_path 會被拿去顯示給使用者。
    """
    first, *rest = source.split("\n")
    if rest and set(rest[-1].strip()) <= {"=", "-"} and rest[-1].strip():
        # setext：標題文字在上一行，底線那行丟掉。
        return first.strip()
    return first.lstrip("#").strip().rstrip("#").strip()

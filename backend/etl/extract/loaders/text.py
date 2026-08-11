"""純文字 loader。

純文字沒有任何結構標記，唯一可靠的訊號是**空行**：它是人寫作時分段的方式。不以單一
換行分段，那會把「一段話因為視窗寬度而斷行」拆成好幾個 block，chunker 拿到的每一塊
都短到沒有語意。
"""

from __future__ import annotations

import re
from typing import Any

from core.exceptions import ExtractionFailedError
from etl.extract.loaders.base import LoadResult
from etl.extract.model import Block, BlockMeta, BlockType

# 空行 = 只有空白字元的一行。純粹 ``\n\n`` 的話，含尾隨空格的空行（編輯器很常見）
# 就分不了段，而肉眼看起來明明分了。
_BLANK_LINE = re.compile(r"\n[ \t\r\f\v]*\n")


def load(content: bytes) -> LoadResult:
    """UTF-8 純文字 → 段落 block。

    解碼失敗直接失敗而不是 ``errors="replace"``：上傳端（`services/knowledge/uploads`）
    已經驗過這是解得開的 UTF-8，這裡再解不開代表兩邊看到的位元組不同——那是儲存或
    傳輸出了問題，用替代字元蓋掉只會讓一份亂碼文件平靜地變成 ready。
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractionFailedError(
            "純文字解碼失敗（非 UTF-8）",
            cause=type(exc).__name__,
        ) from exc

    blocks: list[Block] = []
    for paragraph in _BLANK_LINE.split(text):
        stripped = paragraph.strip()
        if not stripped:
            continue
        blocks.append(
            Block(
                type=BlockType.PARAGRAPH,
                text=stripped,
                # ``page`` 留 None：純文字沒有頁的概念（見 BlockMeta 的說明）。
                meta=BlockMeta(order=len(blocks)),
            )
        )

    doc_meta: dict[str, Any] = {"char_count": len(text)}
    return blocks, doc_meta

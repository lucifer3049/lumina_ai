"""loader 的共同型別與共用小工具。

`HeadingStack` 放在這裡是因為 PDF 與 docx 對「標題」的判定完全不同（字級 vs 樣式），
但**維護 heading_path 的規則一模一樣**：進到更淺的層級就要把深的彈掉。那條規則漏掉
的症狀很好認——文件後半的每一段都掛著前面所有小節的名字，heading_path 變成一條愈來
愈長、引用顯示出來完全不可讀的垃圾。寫兩份就會有一份是錯的。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from etl.extract.model import Block

# loader 回傳 blocks 與**這個來源獨有的** doc_meta（PDF 的頁數之類）；
# 共同的鍵（media_type、block_count）由 `etl.extract.extract` 統一補上，
# 免得每個 loader 各填一次而漏掉其中一個。
type LoadResult = tuple[list[Block], dict[str, Any]]
type Loader = Callable[[bytes], LoadResult]


class HeadingStack:
    """依層級維護目前的標題路徑。

    ``level`` 的來源各 loader 自訂：docx 用樣式名裡的數字（``Heading 2`` → 2），
    PDF 用字級排名。只要「數字愈小愈外層」成立，這裡的邏輯就適用。
    """

    def __init__(self) -> None:
        self._entries: list[tuple[int, str]] = []

    def push(self, level: int, text: str) -> None:
        """記下一個標題；同層或更外層的標題會先被彈掉。"""
        while self._entries and self._entries[-1][0] >= level:
            self._entries.pop()
        self._entries.append((level, text))

    def path(self) -> tuple[str, ...]:
        return tuple(text for _, text in self._entries)

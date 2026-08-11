"""`ExtractedDoc` —— 所有 loader 的統一產出（08 §3）。

**這個型別是 loader 與下游之間唯一的契約。** 08 §1 的「下游完全來源無關」全靠它：
Clean / Chunk / Embed 看到的永遠是一串帶型別與位置的 block，不知道它來自 PDF、
docx 還是 API 回應。新增來源因此只是新增一個 loader，下游一行都不必改。

三個欄位各自對應一種下游需求：

- ``type``：1B-5 的切塊要靠它做「標題邊界優先、表格不拆散」。全部當成等價的文字，
  那兩條規則就沒有依據。
- ``meta.page`` / ``meta.heading_path``：1D 的引用要能說出「出自第 12 頁〈第三章〉」。
- ``meta.order``：閱讀順序。亂了不會有錯誤，只會讓相鄰 chunk 在語意上不相鄰。

不可變（``frozen``）：`ExtractedDoc` 會跨行程邊界（抽取跑在子行程，見 `sandbox`）
並被多個下游階段共讀。允許就地修改的話，「是誰改了這個 block」在跨階段的除錯裡
幾乎追不回來。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class BlockType(StrEnum):
    """08 §3 的 block 型別。

    1B 的三個 loader 只產出前三種；``CODE`` / ``CAPTION`` 先宣告，因為它們是同一個
    列舉的成員而不是新概念——等到有 loader 認得出它們時直接用，不必改動下游的判斷。
    """

    PARAGRAPH = "paragraph"
    HEADING = "heading"
    TABLE = "table"
    CODE = "code"
    CAPTION = "caption"


@dataclass(frozen=True, slots=True)
class BlockMeta:
    """block 的位置資訊。

    ``page`` 用 ``None`` 而不是 0 或 1 表示「這個來源沒有頁的概念」（純文字、API
    回應）。填一個假頁碼的話，引用會顯示一個不存在的頁，使用者點過去只會更困惑。
    """

    order: int
    page: int | None = None
    heading_path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Block:
    """一段內容。``text`` 已去除前後空白，且不會是空字串（loader 負責丟棄空 block）。"""

    type: BlockType
    text: str
    meta: BlockMeta


@dataclass(frozen=True, slots=True)
class ExtractedDoc:
    """一份文件的抽取結果。

    ``doc_meta`` 是**開放**的字典而不是固定欄位：各 loader 能記的統計本來就不同
    （PDF 有頁數、API 有回應筆數），而 08 §4 要求這些數字最後落進 ``etl_jobs.stats``
    ——那本來就是 JSONB。固定欄位會逼每個 loader 填一堆 None。

    共同的鍵有兩個，下游一定拿得到：``media_type``、``block_count``。
    """

    blocks: tuple[Block, ...]
    doc_meta: dict[str, Any] = field(default_factory=dict)

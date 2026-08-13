"""Token 估算 —— 切塊時判斷「這一塊多大」的依據。

**這是估算，不是真的 tokenizer。** 真正的切詞由 embedding／生成模型決定，而模型是
AI Gateway（1C）的事——在 ETL 這一層綁死某個模型的 tokenizer，等於讓 chunk 的大小
依賴一個隨時會換的東西，換模型時整個知識庫的切塊會全部失效。

因此這裡給的是一個**保守的上界估計**，並讓 chunker 接受注入（`count_tokens` 參數）：
1C 有了 Gateway 之後可以把真正的計數器傳進來，chunker 一行都不必改。

估法（對映 cl100k 這類 BPE 的實測經驗值）：CJK 字元約 1 token／字，其餘文字約
4 字元／token。偏高比偏低安全——低估的後果是 embedding 呼叫在執行期被 provider 以
token 上限退回，而那時文件已經處理到一半。
"""

from __future__ import annotations

import math
from collections.abc import Callable

# 落在這些區間的字元視為 CJK（含中日韓統一表意文字、假名、諺文、全形標點）。
_CJK_RANGES = (
    (0x3000, 0x303F),  # CJK 標點
    (0x3040, 0x30FF),  # 平假名、片假名
    (0x3400, 0x4DBF),  # 擴充 A
    (0x4E00, 0x9FFF),  # 統一表意文字
    (0xAC00, 0xD7AF),  # 諺文音節
    (0xF900, 0xFAFF),  # 相容表意文字
    (0xFF00, 0xFFEF),  # 全形字元
)

_CHARS_PER_TOKEN = 4

TokenCounter = Callable[[str], int]


def is_cjk(char: str) -> bool:
    code = ord(char)
    return any(start <= code <= end for start, end in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """文字的 token 數估計值（上界導向）。"""
    if not text:
        return 0
    cjk = sum(1 for char in text if is_cjk(char))
    others = len(text) - cjk
    return cjk + math.ceil(others / _CHARS_PER_TOKEN)

"""在子行程中抽取 —— `extract` 與 `sandbox` 的接合點。

分成獨立模組而不是寫進 `etl.extract.__init__`：forkserver 以 **pickle** 傳遞要執行的
函式，因此那個函式必須是模組層級、且它所在的模組要能在子行程裡乾淨地被 import。
把它放進 `__init__` 會讓子行程連帶 import 整個套件的公開介面。

上限值放在這裡而不是 Pydantic Settings：它們是**這段程式的安全邊界**，不是部署差異
——調鬆會讓毒檔防護失效，而那不該是一個改環境變數就能做到的事。真的需要依租戶調整
時（大檔客戶），入口是 `IngestionService` 的建構參數。
"""

from __future__ import annotations

from functools import partial

from etl.extract import extract
from etl.extract.model import ExtractedDoc
from etl.extract.sandbox import run_isolated

# 單份文件的解析上限（08 §6：超時 kill）。120 秒對 500 頁的 PDF 是充裕的，對無限
# 迴圈則是明確的止血點。抽取跑在 etl 佇列上，這段時間不影響任何請求。
EXTRACT_TIMEOUT_SECONDS = 120.0

# 子行程的位址空間上限。1GB 容得下大型 PDF 的解析，同時擋得住 zip bomb 那類
# 「解壓後 40GB」的檔案（08 §6 的毒檔防護）。
EXTRACT_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024


def extract_isolated(
    content: bytes,
    *,
    media_type: str,
    timeout_seconds: float = EXTRACT_TIMEOUT_SECONDS,
    memory_limit_bytes: int = EXTRACT_MEMORY_LIMIT_BYTES,
) -> ExtractedDoc:
    """跑在子行程裡的 `extract`。失敗一律 `ExtractionFailedError`（含逾時與崩潰）。"""
    return run_isolated(
        partial(_extract, media_type=media_type),
        content,
        timeout_seconds=timeout_seconds,
        memory_limit_bytes=memory_limit_bytes,
    )


def _extract(content: bytes, *, media_type: str) -> ExtractedDoc:
    """子行程的進入點。**必須是模組層級函式**（forkserver 以 pickle 傳遞）。"""
    return extract(content, media_type=media_type)

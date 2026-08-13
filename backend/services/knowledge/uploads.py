"""上傳內容的判定 —— MIME sniffing 與大小上限（10 §99、09 §3.1）。

**判定依內容，不依副檔名。** 副檔名是使用者送來的字串：把執行檔改名成
``report.pdf`` 就能繞過任何以它為準的白名單，而下游的 loader 會拿著「這是 PDF」的
假設去解析。08 §6 的毒檔防護擋的是解析階段的爆炸，擋不住「一開始就不該收」。

**白名單而不是黑名單**：黑名單要窮舉所有危險格式，而新格式一直在出現。白名單的
失敗方向是「合法的東西被擋下」（使用者會回報），黑名單的失敗方向是「危險的東西被
收下」（沒有人會回報）。

**自己判定格式，不引入依賴**（1B-3 決定）：`filetype` 認不出純文字（沒有 magic
bytes），`python-magic` 要 C 函式庫。白名單只有五種，而每一種的判定各只需要幾行
——這裡的程式碼本身就是白名單，讀得完。

**唯一的副檔名例外：Markdown（1B-4b）。** ``.md`` 與 ``.txt`` 的位元組沒有任何差別
——Markdown 就是文字。因此副檔名在這裡決定的**不是能不能收**（內容仍須先通過純文字
的驗證），而是**交給哪個 loader**：走 Markdown loader 才留得住標題與表格結構，走純
文字 loader 則整份文件變成一堆等價的段落。改副檔名能造成的最壞後果，是自己的
Markdown 被當成純文字（或反之）——沒有安全邊界被跨過。
"""

from __future__ import annotations

import hashlib
import io
import zipfile

from core.exceptions import UnsupportedMediaTypeError, UploadTooLargeError
from core.media_types import DOCX, MARKDOWN, PDF, TEXT, XLSX

# 常數本身移到 `core/media_types.py`（1B-4）：抽取端要用同一組鍵挑 loader，而
# `etl/` 不得 import `services/`。這裡沿用原本的名字再匯出，呼叫端與測試不受影響。
__all__ = [
    "ACCEPTED_MEDIA_TYPES",
    "DOCX",
    "MARKDOWN",
    "MAX_UPLOAD_BYTES",
    "PDF",
    "TEXT",
    "XLSX",
    "detect_media_type",
    "ensure_within_limit",
    "sha256_of",
]

ACCEPTED_MEDIA_TYPES = (PDF, DOCX, XLSX, TEXT, MARKDOWN)

# 09 §3.1 的界線：>32MB 走 presigned 分塊直傳。上限若訂得比界線高，就會有一段
# 「兩條路都說該走另一條」的區間；分塊流程尚未實作，因此該區間的正確行為是 413。
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
# Office Open XML 各自的必要成員。ZIP 魔術字只說明「這是個 zip」——docx、xlsx、jar、
# 以及 zip bomb 全都長一樣，所以要往容器裡再看一層。
_DOCX_MARKER = "word/document.xml"
_XLSX_MARKER = "xl/workbook.xml"

# 走 Markdown loader 的副檔名（見模組 docstring 對這個例外的說明）。
_MARKDOWN_SUFFIXES = (".md", ".markdown")

# 純文字允許的控制字元：定位、換行、回車、換頁。其餘控制字元（尤其 NUL）出現時，
# 那多半是解得開 UTF-8 的二進位檔——大量位元組落在 ASCII 範圍就會發生。
_ALLOWED_CONTROL_CHARS = {"\t", "\n", "\r", "\f", "\v"}


def sha256_of(content: bytes) -> str:
    """去重鍵（05 §3.2）。與檔名無關：同內容不同檔名算重複，改一個字就是新文件。"""
    return hashlib.sha256(content).hexdigest()


def detect_media_type(content: bytes, *, filename: str) -> str:
    """回傳白名單內的 media type，判不出來就 raise 415。

    ``filename`` **只在內容已確定是純文字之後**才被看一眼，用來區分 Markdown 與
    純文字（見模組 docstring）。二進位格式一律以內容判定，副檔名完全不參與。
    """
    if not content:
        # 空檔沒有內容可判定，對下游也沒有意義（chunk 數為 0 的文件）。
        raise UnsupportedMediaTypeError(accepted=ACCEPTED_MEDIA_TYPES)

    if content.startswith(_PDF_MAGIC):
        return PDF

    if content.startswith(_ZIP_MAGIC):
        if _zip_contains(content, _DOCX_MARKER):
            return DOCX
        if _zip_contains(content, _XLSX_MARKER):
            return XLSX

    if _looks_like_text(content):
        return MARKDOWN if _has_markdown_suffix(filename) else TEXT

    raise UnsupportedMediaTypeError(accepted=ACCEPTED_MEDIA_TYPES)


def _zip_contains(content: bytes, marker: str) -> bool:
    """zip 容器內是否有指定成員。

    壞掉的 zip 直接回 False 而不是往上冒——那同樣是「不是我們接受的格式」，
    而 ``BadZipFile`` 冒到 handler 就變成 500。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return marker in archive.namelist()
    except zipfile.BadZipFile:
        return False


def _has_markdown_suffix(filename: str) -> bool:
    return filename.lower().endswith(_MARKDOWN_SUFFIXES)


def _looks_like_text(content: bytes) -> bool:
    """解得開 UTF-8 且不含意外的控制字元。

    兩個條件缺一不可：只驗 decode 的話，很多二進位檔會被收成 text/plain
    （PE、ELF 的大部分位元組落在 ASCII 範圍，碰巧解得開的機率不低）。
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return False

    return not any(
        char.isprintable() is False and char not in _ALLOWED_CONTROL_CHARS for char in text
    )


def ensure_within_limit(size_bytes: int) -> None:
    """超過單請求上限就 413。

    分出一個函式而不是寫在服務裡：上限的判定要發生在**寫進物件儲存之前**，
    而「驗證在哪一步」正是這條規則唯一會出錯的地方。
    """
    if size_bytes > MAX_UPLOAD_BYTES:
        raise UploadTooLargeError(limit_bytes=MAX_UPLOAD_BYTES)

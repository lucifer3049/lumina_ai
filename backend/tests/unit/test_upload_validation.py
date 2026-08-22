"""驗收：上傳的內容判定（10 §99：MIME sniffing 走 magic bytes，非副檔名）。

**為什麼不能信副檔名**：它是使用者送來的字串。把 `evil.exe` 改名成 `report.pdf`
就能繞過任何以副檔名為準的白名單，而下游的 loader 會拿著「這是 PDF」的假設去
解析——08 §6 的毒檔防護擋的是解析階段的爆炸，擋不住「一開始就不該收」。

**為什麼白名單而不是黑名單**：黑名單要窮舉所有危險格式，而新格式一直在出現。
白名單的失敗方向是「合法的東西被擋下」（使用者會回報），黑名單的失敗方向是
「危險的東西被收下」（沒有人會回報）。

白名單是 PDF / docx / xlsx / txt / Markdown（08 §3 的 loader；xlsx 與 Markdown 於
1B-4b 提前自 2D）。純邏輯、不碰 DB 也不碰物件儲存，因此放 unit 層。

**Markdown 是唯一看副檔名的型別**，而它不構成上面那條規則的例外：Markdown 與純文字
的位元組完全相同，副檔名決定的是「交給哪個 loader」而不是「收不收」——內容仍須先
通過純文字驗證。`TestMarkdownSuffix` 把這個邊界釘住。
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from core.exceptions import UnsupportedMediaTypeError, UploadTooLargeError
from services.knowledge.uploads import MAX_UPLOAD_BYTES, detect_media_type, sha256_of

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
TEXT_BYTES = "第一行\n第二行\n".encode()


def _docx_bytes() -> bytes:
    """最小的 .docx：ZIP 容器 + Office Open XML 的必要成員。

    docx 的 magic bytes 就是 ZIP 的 ``PK\\x03\\x04``——**光看前四個位元組分不出
    docx、xlsx、jar 還是任何一個 zip 檔**。因此判定要再往裡看一層：容器內必須有
    ``word/document.xml``。少了這一步，白名單等於「接受任何 zip」，而 zip bomb 正是
    08 §6 點名的毒檔之一。
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    return buffer.getvalue()


def _xlsx_bytes() -> bytes:
    """最小的 .xlsx：同樣是 ZIP 容器，但必要成員是 ``xl/workbook.xml``。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return buffer.getvalue()


class TestAcceptedTypes:
    def test_pdf_is_detected_from_magic_bytes(self) -> None:
        assert detect_media_type(PDF_BYTES, filename="anything.bin") == "application/pdf"

    def test_docx_is_detected_by_looking_inside_the_zip(self) -> None:
        assert detect_media_type(_docx_bytes(), filename="x.zip") == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    def test_plain_text_is_accepted(self) -> None:
        """txt 沒有 magic bytes——判定方式是「解得開 UTF-8 且不含控制字元」。

        這是白名單裡唯一一個「以排除法認定」的型別，所以它的邊界要特別小心：
        任何二進位檔只要碰巧解得開 UTF-8 就會被當成文字。下面兩條測試守住那個邊界。
        """
        assert detect_media_type(TEXT_BYTES, filename="notes.txt") == "text/plain"


class TestSpreadsheetsAndMarkdown:
    """1B-4b 新增的兩種型別。"""

    def test_xlsx_is_detected_by_looking_inside_the_zip(self) -> None:
        """xlsx 與 docx 的前四個位元組相同，靠容器內的成員區分。

        只驗 ZIP 魔術字就當成 xlsx 的話，一份 docx 會被送進試算表 loader——
        失敗訊息會指向解析，而真正的錯誤發生在判定。
        """
        assert detect_media_type(_xlsx_bytes(), filename="x.zip") == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    def test_markdown_suffix_selects_the_markdown_loader(self) -> None:
        assert detect_media_type(b"# title\n", filename="notes.md") == "text/markdown"

    def test_same_bytes_without_the_suffix_stay_plain_text(self) -> None:
        """同一份位元組換個副檔名就是純文字——這正是它必須看副檔名的原因。"""
        assert detect_media_type(b"# title\n", filename="notes.txt") == "text/plain"

    def test_markdown_suffix_does_not_smuggle_binaries(self) -> None:
        """``.md`` 不會讓二進位內容過關——副檔名只在內容已是純文字之後才被看。

        這條是本檔第一條規則（不信副檔名）在新型別上的延伸：Markdown 的例外只影響
        「選哪個 loader」，不影響「收不收」。
        """
        with pytest.raises(UnsupportedMediaTypeError):
            detect_media_type(b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64, filename="evil.md")


class TestRejectedContent:
    def test_extension_does_not_override_content(self) -> None:
        """副檔名是 ``.pdf`` 但內容是執行檔——必須拒絕。

        **這是本檔最重要的一條**：以副檔名為準的實作會在這裡放行，而且下游完全不會
        察覺——loader 拿到「PDF」去解析，失敗後只會記一筆 ETL 錯誤。
        """
        windows_executable = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64

        with pytest.raises(UnsupportedMediaTypeError):
            detect_media_type(windows_executable, filename="report.pdf")

    def test_a_plain_zip_is_not_a_docx(self) -> None:
        """ZIP 魔術字相符但裡面沒有 ``word/document.xml``——不是 docx。

        只比對 ``PK\\x03\\x04`` 的實作會放行任何 zip，包含 zip bomb。
        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("hello.txt", "not a docx")

        with pytest.raises(UnsupportedMediaTypeError):
            detect_media_type(buffer.getvalue(), filename="doc.docx")

    def test_binary_that_is_not_valid_utf8_is_rejected(self) -> None:
        with pytest.raises(UnsupportedMediaTypeError):
            detect_media_type(b"\xff\xfe\x00\x01\x02\x03", filename="notes.txt")

    def test_text_with_control_characters_is_rejected(self) -> None:
        """含 NUL 之類控制字元的「文字」不收。

        很多二進位格式解得開 UTF-8（大量位元組落在 ASCII 範圍），單靠 decode 判定
        會把它們全部收成 text/plain。控制字元是最便宜且有效的第二道條件。
        """
        with pytest.raises(UnsupportedMediaTypeError):
            detect_media_type(b"hello\x00\x01world", filename="notes.txt")

    def test_empty_file_is_rejected(self) -> None:
        """空檔沒有內容可判定，而且對下游沒有意義（chunk 數為 0 的文件）。"""
        with pytest.raises(UnsupportedMediaTypeError):
            detect_media_type(b"", filename="empty.txt")


class TestContentHash:
    def test_hash_is_sha256_of_the_bytes(self) -> None:
        """去重鍵是內容的 SHA-256（05 §3.2），與檔名無關。

        同一份文件用不同檔名上傳兩次應該被判定為重複；反過來，改了一個字的文件
        即使檔名相同也是新文件。
        """
        import hashlib

        assert sha256_of(PDF_BYTES) == hashlib.sha256(PDF_BYTES).hexdigest()
        assert sha256_of(PDF_BYTES) != sha256_of(PDF_BYTES + b" ")


def test_single_request_limit_matches_the_chunked_upload_threshold() -> None:
    """單請求上限 = 32MB（09 §3.1 的分塊界線）。

    這個數字不是隨手挑的：09 §3.1 規定 >32MB 走 presigned 分塊直傳。上限若訂得比
    界線高，就會有一段「文件大小介於兩者之間」的區間，兩條路都說該走另一條。分塊
    流程尚未實作，所以這個區間現在的正確行為是 413 並在訊息裡說明上限。
    """
    assert MAX_UPLOAD_BYTES == 32 * 1024 * 1024


class TestChunkedRead:
    """`_read_within_limit`（`api/v1/knowledge.py`）——**擋在載回記憶體之前**。

    原本是 `await file.read()`：一次把整份內容變成 bytes，而大小判定在 service 裡才跑。
    32MB 的上限擋的是「收不收」，擋不了「先吃掉多少記憶體」——幾個併發的 2GB 上傳就是
    一次 OOM，而 uvicorn 預設沒有 body 大小限制。
    """

    @staticmethod
    def _file(content: bytes, *, size: int | None) -> Any:
        from fastapi import UploadFile

        return UploadFile(file=io.BytesIO(content), size=size, filename="x.pdf")

    async def test_a_normal_file_comes_back_whole(self) -> None:
        from api.v1.knowledge import _read_within_limit

        content = b"%PDF-1.7\n" + b"0" * 1000

        assert await _read_within_limit(self._file(content, size=len(content))) == content

    async def test_a_declared_oversize_is_rejected_without_reading(self) -> None:
        """`file.size` 有值時連讀都不必讀——multipart 解析器已經數過了。"""
        from api.v1.knowledge import _read_within_limit

        class _Explodes(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:  # pragma: no cover —— 不該被呼叫
                raise AssertionError("已知過大的內容不該被讀進來")

        from fastapi import UploadFile

        upload = UploadFile(file=_Explodes(b""), size=MAX_UPLOAD_BYTES + 1, filename="x.pdf")

        with pytest.raises(UploadTooLargeError):
            await _read_within_limit(upload)

    async def test_an_undeclared_oversize_is_stopped_mid_read(self) -> None:
        """`Content-Length` 缺席（chunked）或說謊時 `size` 是 None——這時只能邊讀邊數，
        但**超過的那一刻就停**，不是讀完再判斷。"""
        from api.v1.knowledge import _read_within_limit

        oversized = b"0" * (MAX_UPLOAD_BYTES + 1)

        with pytest.raises(UploadTooLargeError):
            await _read_within_limit(self._file(oversized, size=None))

    async def test_the_limit_itself_is_inclusive(self) -> None:
        """剛好等於上限要收——邊界寫錯的話，一個剛好 32MB 的合法檔案會被拒。"""
        from api.v1.knowledge import _read_within_limit

        exact = b"0" * MAX_UPLOAD_BYTES

        assert len(await _read_within_limit(self._file(exact, size=None))) == MAX_UPLOAD_BYTES

"""驗收：上傳的內容判定（10 §99：MIME sniffing 走 magic bytes，非副檔名）。

**為什麼不能信副檔名**：它是使用者送來的字串。把 `evil.exe` 改名成 `report.pdf`
就能繞過任何以副檔名為準的白名單，而下游的 loader 會拿著「這是 PDF」的假設去
解析——08 §6 的毒檔防護擋的是解析階段的爆炸，擋不住「一開始就不該收」。

**為什麼白名單而不是黑名單**：黑名單要窮舉所有危險格式，而新格式一直在出現。
白名單的失敗方向是「合法的東西被擋下」（使用者會回報），黑名單的失敗方向是
「危險的東西被收下」（沒有人會回報）。

1B 的白名單是 PDF / docx / txt（13 §3 的三種 loader）。純邏輯、不碰 DB 也不碰
物件儲存，因此放 unit 層。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from core.exceptions import UnsupportedMediaTypeError
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

"""驗收：三種 loader → 統一的 `ExtractedDoc`（08 §3、13 §3 工作包 1B-4）。

**loader 的職責邊界只有一件事**：來源 → 統一中間格式。清洗（頁首頁尾偵測、亂碼
丟棄）屬 Clean、切塊屬 Chunk，兩者都在 1B-5——混進 loader 的話，每加一種來源就要
重寫一次同樣的清洗規則，而 08 §1 的「下游完全來源無關」也就不成立了。

因此本檔驗的是**結構保真**，不是內容品質：

1. **block 型別分得出來**（paragraph / heading / table）。分不出來的話，1B-5 的
   「標題邊界優先、表格不拆散」就沒有依據可用——chunker 只會看到一堆等價的文字。
2. **標題階層（heading_path）**：chunk 的 meta 要靠它回答「這段話出自哪一節」，
   而那是引用能不能指到正確位置的前提。
3. **頁碼**：PDF 的引用要標頁數（09 §3.2 的 citations event 有 page 欄位）。
4. **順序（order）**：block 的先後就是文件的閱讀順序。亂了不會有錯誤，只會讓答案
   引用到語意上不相鄰的段落。

測試用的樣本檔在測試裡**即時產生**而不是放進版控：二進位樣本進 repo 之後沒有人
知道它們裡面有什麼，改壞了也看不出來；用程式產生的話，「這份 PDF 有兩頁、第二頁
有一個標題」是讀得到的。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from core.exceptions import ExtractionFailedError
from etl.extract import extract
from etl.extract.model import BlockType, ExtractedDoc
from services.knowledge.uploads import DOCX, PDF, TEXT


def _pdf(pages: list[list[tuple[str, int]]]) -> bytes:
    """產生 PDF：每頁一串 (文字, 字級)。字級大的視為標題（見 loader 的判定說明）。"""
    import pymupdf

    document = pymupdf.open()
    for lines in pages:
        page = document.new_page()
        y = 72.0
        for text, size in lines:
            page.insert_text((72, y), text, fontsize=size)
            y += size * 2
    data: bytes = document.tobytes()
    document.close()
    return data


def _docx(paragraphs: list[tuple[str, str]], tables: list[list[list[str]]] | None = None) -> bytes:
    """產生 docx：段落是 (文字, 樣式名)，樣式 ``Heading 1`` 之類代表標題。"""
    import docx

    document = docx.Document()
    for text, style in paragraphs:
        document.add_paragraph(text, style=style or None)
    for rows in tables or []:
        table = document.add_table(rows=len(rows), cols=len(rows[0]))
        for row_index, row in enumerate(rows):
            for cell_index, value in enumerate(row):
                table.cell(row_index, cell_index).text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class TestPlainText:
    def test_paragraphs_are_split_on_blank_lines(self) -> None:
        """空行分段——純文字唯一可靠的結構訊號。

        不以單一換行分段：那會把「一段話因為視窗寬度而斷行」拆成好幾個 block，
        而 chunker 拿到的每一塊都短到沒有語意。
        """
        content = "第一段第一行\n第一段第二行\n\n第二段\n".encode()

        doc = extract(content, media_type=TEXT)

        assert [block.text for block in doc.blocks] == ["第一段第一行\n第一段第二行", "第二段"]
        assert {block.type for block in doc.blocks} == {BlockType.PARAGRAPH}

    def test_order_is_preserved(self) -> None:
        content = "\n\n".join(f"段落{i}" for i in range(5)).encode()

        doc = extract(content, media_type=TEXT)

        assert [block.meta.order for block in doc.blocks] == [0, 1, 2, 3, 4]

    def test_text_has_no_page_numbers(self) -> None:
        """純文字沒有頁的概念——``page`` 必須是 None，不是 0 或 1。

        填一個假頁碼的話，引用會顯示「第 1 頁」而那個頁不存在；使用者點過去只會
        更困惑。沒有的資訊就要表達成沒有。
        """
        doc = extract(b"only one paragraph", media_type=TEXT)

        assert doc.blocks[0].meta.page is None


class TestPdf:
    def test_text_and_page_numbers_are_extracted(self) -> None:
        content = _pdf([[("第一頁的內容", 11)], [("第二頁的內容", 11)]])

        doc = extract(content, media_type=PDF)

        texts = {block.meta.page: block.text for block in doc.blocks}
        assert "第一頁的內容" in texts[1]
        assert "第二頁的內容" in texts[2]

    def test_page_numbers_are_one_based(self) -> None:
        """頁碼從 1 開始——那是使用者看到的編號。

        pymupdf 的頁索引是 0-based，直接沿用會讓引用整份文件差一頁，而每一個
        引用看起來都很合理（只是內容對不上）。
        """
        doc = extract(_pdf([[("唯一一頁", 11)]]), media_type=PDF)

        assert {block.meta.page for block in doc.blocks} == {1}

    def test_larger_font_becomes_a_heading(self) -> None:
        """字級明顯大於內文的行視為標題。

        PDF 沒有語意標記，只有視覺屬性——標題偵測必然是啟發式的。用字級而不是粗體
        或位置，是因為字級是三者中最少誤判的：粗體常用於強調，而位置對雙欄排版
        完全失效。判錯的後果是 heading_path 不準（引用指到相鄰的節），不是壞掉。
        """
        content = _pdf([[("第一章 總則", 22), ("本章說明適用範圍。", 11)]])

        doc = extract(content, media_type=PDF)
        headings = [block for block in doc.blocks if block.type is BlockType.HEADING]

        assert [block.text for block in headings] == ["第一章 總則"]

    def test_body_blocks_carry_the_heading_path(self) -> None:
        """內文的 ``heading_path`` 是它上方所有標題的堆疊。

        這是 1B-5 切塊與 1D 引用共同的依據：使用者看到的是「出自〈第一章 總則〉」，
        而不是一個沒有上下文的段落。
        """
        content = _pdf([[("第一章 總則", 22), ("本章說明適用範圍。", 11)]])

        doc = extract(content, media_type=PDF)
        body = next(block for block in doc.blocks if block.type is BlockType.PARAGRAPH)

        assert body.meta.heading_path == ("第一章 總則",)

    def test_page_count_is_reported_in_doc_meta(self) -> None:
        """頁數進 ``doc_meta``——08 §4 的 stats 要它，而且是毒檔上限的判定依據。"""
        doc = extract(_pdf([[("a", 11)], [("b", 11)], [("c", 11)]]), media_type=PDF)

        assert doc.doc_meta["page_count"] == 3


class TestDocx:
    def test_headings_are_detected_from_styles(self) -> None:
        """docx 的標題是**樣式**，不是猜的——這是它與 PDF 最大的差別。

        因此 docx 的 heading_path 可以完全信任，而 PDF 的是啟發式的。兩者產出同一個
        欄位，但可信度不同；1C 評估檢索品質時要記得這件事。
        """
        content = _docx(
            [
                ("第一章 總則", "Heading 1"),
                ("第一節 目的", "Heading 2"),
                ("本節說明立法目的。", ""),
            ]
        )

        doc = extract(content, media_type=DOCX)
        body = next(block for block in doc.blocks if block.type is BlockType.PARAGRAPH)

        assert body.meta.heading_path == ("第一章 總則", "第一節 目的")

    def test_heading_path_pops_when_the_level_goes_back_up(self) -> None:
        """回到上層標題時，下層要從路徑裡拿掉。

        只往裡疊不往外彈的實作，會讓文件後半的每一段都掛著前面所有小節的名字——
        heading_path 變成一條愈來愈長的垃圾，引用顯示出來完全不可讀。
        """
        content = _docx(
            [
                ("第一章", "Heading 1"),
                ("第一節", "Heading 2"),
                ("甲段", ""),
                ("第二章", "Heading 1"),
                ("乙段", ""),
            ]
        )

        doc = extract(content, media_type=DOCX)
        paragraphs = [b for b in doc.blocks if b.type is BlockType.PARAGRAPH]

        assert paragraphs[0].meta.heading_path == ("第一章", "第一節")
        assert paragraphs[1].meta.heading_path == ("第二章",)

    def test_tables_become_table_blocks_with_the_header_kept(self) -> None:
        """表格是獨立的 block 型別，且表頭要留著。

        表頭決定每一欄的意義。把表格攤平成純文字之後，「2025 / 3.2 / 通過」這種列
        對 LLM 毫無意義；1B-5 的「表格不拆散」與 1C 的檢索都需要它。
        """
        content = _docx(
            [("規格表", "Heading 1")],
            tables=[[["項目", "數值"], ["延遲", "300ms"]]],
        )

        doc = extract(content, media_type=DOCX)
        tables = [block for block in doc.blocks if block.type is BlockType.TABLE]

        assert len(tables) == 1
        assert "項目" in tables[0].text and "延遲" in tables[0].text
        assert tables[0].meta.heading_path == ("規格表",)

    def test_empty_paragraphs_are_dropped(self) -> None:
        """空段落不產生 block——它們是排版產物，不是內容。"""
        content = _docx([("有內容", ""), ("", ""), ("   ", ""), ("也有內容", "")])

        doc = extract(content, media_type=DOCX)

        assert [block.text for block in doc.blocks] == ["有內容", "也有內容"]


class TestUnsupportedAndBroken:
    def test_unknown_media_type_raises(self) -> None:
        """沒有 loader 的型別要明確失敗。

        08 §1：新增來源 = 新增一個 loader。回空的 `ExtractedDoc` 會讓文件走完整條
        ETL 並停在 ready，而它一個 chunk 都沒有——使用者問問題時只會覺得「這份文件
        好像沒有被讀進去」。
        """
        with pytest.raises(ExtractionFailedError):
            extract(b"%PDF-1.7\n", media_type="application/x-tar")

    def test_corrupted_pdf_raises_extraction_failed(self) -> None:
        """壞掉的檔案 → `ExtractionFailedError`，不是函式庫自己的例外。

        pymupdf / python-docx 的例外冒到上層之後，08 §6 的重試判定就得認得第三方
        的例外型別——而那會隨版本改變。轉成自家例外，重試與 DLQ 的規則才有穩定依據。
        """
        with pytest.raises(ExtractionFailedError):
            extract("%PDF-1.7\n這不是一個真的 PDF".encode(), media_type=PDF)

    def test_docx_without_document_xml_raises(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("hello.txt", "not a docx")

        with pytest.raises(ExtractionFailedError):
            extract(buffer.getvalue(), media_type=DOCX)


class TestExtractedDocShape:
    def test_doc_meta_records_the_source_media_type(self) -> None:
        """`doc_meta` 要記下它是從什麼格式來的（08 §3）。

        下游（Clean、Chunk、評估）會依格式調整策略——例如 PDF 的頁首頁尾偵測對
        純文字沒有意義。少了這個欄位，那些判斷只能靠猜或再看一次原檔。
        """
        doc = extract(b"hello", media_type=TEXT)

        assert isinstance(doc, ExtractedDoc)
        assert doc.doc_meta["media_type"] == TEXT

    def test_block_count_is_recorded(self) -> None:
        """08 §4 的 stats：block 數是「丟棄率」的分母，1B-5 要用。"""
        doc = extract("一\n\n二\n\n三".encode(), media_type=TEXT)

        assert doc.doc_meta["block_count"] == 3

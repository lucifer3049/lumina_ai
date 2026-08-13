"""驗收：Clean 階段（06 §2.1、08 §4、13 §3 工作包 1B-5）。

Clean 站在 loader 與 chunker 之間，職責是**丟掉不是內容的東西**，而不是改寫內容。
三件事各自對應一種若不做就會污染整個知識庫的雜訊：

1. **跨頁重複的頁首頁尾**——每一頁都有的那行「XX 公司機密文件」。不去掉的話它會出現
   在幾乎每個 chunk 裡，於是每一次檢索它都命中，而它從來不是答案。
2. **亂碼 block**——編碼壞掉或抽取失敗的殘骸。它們會被照樣嵌入，佔用向量空間卻永遠
   不該被檢索到；而且丟棄率本身是**來源品質的訊號**（08 §4：> 20% 要示警）。
3. **語言**——寫進 doc_meta，供 embedding 模型選擇與跨語言檢索觀測（06 §3.4）。

**不做的事同樣重要**：不改寫使用者的文字（正規化只處理空白與零寬字元），不動表格的
GFM 結構。引用要能指回原文，而任何「聰明」的改寫都會讓那個對照悄悄失準。
"""

from __future__ import annotations

from etl.clean import HIGH_DROP_RATE, clean
from etl.extract.model import Block, BlockMeta, BlockType, ExtractedDoc


def _doc(*blocks: Block, doc_meta: dict[str, object] | None = None) -> ExtractedDoc:
    meta = dict(doc_meta or {"media_type": "text/plain"})
    return ExtractedDoc(blocks=tuple(blocks), doc_meta=meta)


def _paragraph(text: str, *, order: int = 0, page: int | None = None) -> Block:
    return Block(type=BlockType.PARAGRAPH, text=text, meta=BlockMeta(order=order, page=page))


class TestRepeatedHeadersAndFooters:
    def test_a_line_repeated_on_every_page_is_removed(self) -> None:
        doc = _doc(
            _paragraph("機密文件請勿外流", order=0, page=1),
            _paragraph("第一頁的正文內容在這裡。", order=1, page=1),
            _paragraph("機密文件請勿外流", order=2, page=2),
            _paragraph("第二頁的正文內容在這裡。", order=3, page=2),
            _paragraph("機密文件請勿外流", order=4, page=3),
            _paragraph("第三頁的正文內容在這裡。", order=5, page=3),
        )

        cleaned, _ = clean(doc)

        assert all("機密文件" not in block.text for block in cleaned.blocks)
        assert len(cleaned.blocks) == 3

    def test_text_that_appears_once_is_kept(self) -> None:
        """只出現在一頁的短句不是頁首——它可能就是答案。"""
        doc = _doc(
            _paragraph("重要通知", order=0, page=1),
            _paragraph("其他內容", order=1, page=2),
            _paragraph("再其他內容", order=2, page=3),
        )

        cleaned, _ = clean(doc)

        assert "重要通知" in [block.text for block in cleaned.blocks]

    def test_long_repeated_text_is_kept(self) -> None:
        """重複出現的**長**段落不當頁首處理。

        頁首頁尾的特徵是短。長段落重複出現通常是真的重複內容（條款、免責聲明），
        那是使用者可能會問的東西——判成雜訊刪掉的話，問了會得到「查無資料」。
        """
        clause = "本條款適用於所有使用者，且於服務期間持續有效，違反者本公司得逕行終止服務。"
        doc = _doc(
            _paragraph(clause, order=0, page=1),
            _paragraph(clause, order=1, page=2),
            _paragraph(clause, order=2, page=3),
        )

        cleaned, _ = clean(doc)

        assert len(cleaned.blocks) == 3

    def test_documents_without_pages_are_left_alone(self) -> None:
        """沒有頁碼的來源（txt / Markdown / 試算表）不做頁首偵測。

        「跨頁重複」的前提是有頁。純文字裡重複出現的短行是清單項或標題，刪掉它們
        會把真正的內容挖掉。
        """
        doc = _doc(
            _paragraph("項目一", order=0),
            _paragraph("項目一", order=1),
            _paragraph("項目一", order=2),
        )

        cleaned, _ = clean(doc)

        assert len(cleaned.blocks) == 3


class TestGarbledBlocks:
    def test_a_block_of_replacement_characters_is_dropped(self) -> None:
        doc = _doc(
            _paragraph("正常的一段內容，看得懂。", order=0),
            _paragraph("��������", order=1),
        )

        cleaned, stats = clean(doc)

        assert [block.text for block in cleaned.blocks] == ["正常的一段內容，看得懂。"]
        assert stats.dropped_blocks == 1

    def test_drop_rate_is_reported(self) -> None:
        """丟棄率進 stats（08 §4）——它是**來源品質**的訊號，不只是內部計數。"""
        doc = _doc(
            _paragraph("正常內容一", order=0),
            _paragraph("�" * 20, order=1),
        )

        _, stats = clean(doc)

        assert stats.total_blocks == 2
        assert stats.drop_rate == 0.5

    def test_high_drop_rate_raises_a_quality_warning(self) -> None:
        """> 20% 自動示警（08 §4）：通知使用者檢查來源，而不是安靜地產出半份知識庫。"""
        blocks = [_paragraph("正常內容", order=0)] + [
            _paragraph("�" * 20, order=i) for i in range(1, 4)
        ]

        _, stats = clean(_doc(*blocks))

        assert stats.drop_rate > HIGH_DROP_RATE
        assert stats.quality_warning is True

    def test_a_clean_document_has_no_warning(self) -> None:
        _, stats = clean(_doc(_paragraph("完全正常的內容", order=0)))

        assert stats.dropped_blocks == 0
        assert stats.quality_warning is False


class TestNormalisation:
    def test_zero_width_characters_are_removed(self) -> None:
        """零寬字元看不見，卻會讓同一句話的 hash 與比對全部對不上。"""
        doc = _doc(_paragraph("正​常﻿內容", order=0))

        cleaned, _ = clean(doc)

        assert cleaned.blocks[0].text == "正常內容"

    def test_trailing_whitespace_is_trimmed_per_line(self) -> None:
        doc = _doc(_paragraph("第一行   \n第二行\t", order=0))

        cleaned, _ = clean(doc)

        assert cleaned.blocks[0].text == "第一行\n第二行"

    def test_wording_is_never_rewritten(self) -> None:
        """只動空白與零寬字元。全形轉半形之類的「正規化」會讓引用對不回原文。"""
        original = "價格是 ＄100，約 3.5 折；see http://example.com/a_b"
        doc = _doc(_paragraph(original, order=0))

        cleaned, _ = clean(doc)

        assert cleaned.blocks[0].text == original

    def test_table_structure_is_untouched(self) -> None:
        """表格的 GFM 由 loader 產出，Clean 不得改動它的格線。"""
        table_text = "| 項目 | 數值 |\n| --- | --- |\n| 延遲 | 300ms |"
        doc = _doc(Block(type=BlockType.TABLE, text=table_text, meta=BlockMeta(order=0)))

        cleaned, _ = clean(doc)

        assert cleaned.blocks[0].text == table_text


class TestLanguage:
    def test_chinese_document_is_detected(self) -> None:
        doc = _doc(_paragraph("這是一份中文文件，內容全部都是中文。", order=0))

        cleaned, stats = clean(doc)

        assert cleaned.doc_meta["language"] == "zh"
        assert stats.language == "zh"

    def test_japanese_is_not_mistaken_for_chinese(self) -> None:
        """假名是決定性證據——只看漢字的話，日文會被判成中文而選錯 embedding 模型。"""
        doc = _doc(_paragraph("これは日本語の文書です。内容はすべて日本語。", order=0))

        cleaned, _ = clean(doc)

        assert cleaned.doc_meta["language"] == "ja"

    def test_english_is_detected(self) -> None:
        """拉丁字母的語言靠字元判不出來（英、德、法的字母集合幾乎相同），因此走模型。"""
        doc = _doc(_paragraph("This document is written in English prose.", order=0))

        cleaned, _ = clean(doc)

        assert cleaned.doc_meta["language"] == "en"

    def test_german_is_not_mistaken_for_english(self) -> None:
        doc = _doc(
            _paragraph(
                "Dieses Dokument ist auf Deutsch geschrieben und enthält mehrere Sätze.",
                order=0,
            )
        )

        cleaned, _ = clean(doc)

        assert cleaned.doc_meta["language"] == "de"

    def test_low_confidence_falls_back_to_undetermined(self) -> None:
        """判不準時回 ``und``，不採用模型的首選。

        py3langid 對任何輸入都會給一個答案——只有一個字母的封面頁也會得到 `en`。
        06 §3.4 的跨語言統計依這個欄位分組，隨機的標籤會讓那份數據失效，而
        「不知道」是誠實且**可分辨**的值：查詢時看得出來這份文件沒有可信的語言。
        """
        doc = _doc(_paragraph("a", order=0))

        cleaned, _ = clean(doc)

        assert cleaned.doc_meta["language"] == "und"

    def test_original_doc_meta_is_preserved(self) -> None:
        doc = _doc(
            _paragraph("內容", order=0),
            doc_meta={"media_type": "application/pdf", "page_count": 3},
        )

        cleaned, _ = clean(doc)

        assert cleaned.doc_meta["media_type"] == "application/pdf"
        assert cleaned.doc_meta["page_count"] == 3


class TestOrdering:
    def test_order_is_renumbered_after_dropping(self) -> None:
        """丟棄後 ``order`` 要重新編號，保持連續。

        留著洞的話，下游「這是第幾個 block」的計算會與實際位置不一致，而那種偏差
        不會報錯——只會讓 chunk 的鄰接關係悄悄錯位。
        """
        doc = _doc(
            _paragraph("保留一", order=0),
            _paragraph("�" * 20, order=1),
            _paragraph("保留二", order=2),
        )

        cleaned, _ = clean(doc)

        assert [block.meta.order for block in cleaned.blocks] == [0, 1]

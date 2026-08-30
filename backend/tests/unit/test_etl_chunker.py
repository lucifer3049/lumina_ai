"""驗收：recursive chunker（06 §2.1、08 §4、13 §3 工作包 1B-5）。

**chunk 是檢索的單位，也是引用的單位。** 因此它同時要滿足兩件互相拉扯的事：夠大
（含得下判斷語意所需的上下文）、夠小（塞得進 token 預算，且命中時精準）。

策略是**結構感知的遞迴切塊**（06 §2.1 預設 recursive，target 512 / overlap 64）：

1. **標題邊界優先**——跨節的兩段話放進同一塊，會讓檢索命中後拿到兩個主題混在一起
   的內容，而 LLM 沒有辦法分辨哪一半才是答案。
2. **表格不拆散**——一張表被切成兩半之後，後半沒有表頭，每一列都失去欄位意義。
3. **超長段落才遞迴往下切**（段落 → 句子 → 硬切）。先試大的邊界、切不動才降級，
   是這個策略叫 recursive 的原因。
4. **重疊**——被迫從中間切開時，下一塊帶上一塊的尾巴，答案剛好落在切點上時才不會
   兩塊都只有一半。

chunk 的文字是 **Markdown**（`etl.extract.markdown` 的序列化），meta 仍帶頁碼與
heading_path——1D 的引用要指得出「第幾頁、哪一節」。
"""

from __future__ import annotations

import re

from etl.chunk import ChunkConfig, chunk_document
from etl.extract.model import Block, BlockMeta, BlockType, ExtractedDoc


def _doc(*blocks: Block) -> ExtractedDoc:
    return ExtractedDoc(blocks=tuple(blocks), doc_meta={"media_type": "text/plain"})


def _heading(
    text: str, *, order: int, ancestors: tuple[str, ...] = (), page: int | None = None
) -> Block:
    return Block(
        type=BlockType.HEADING,
        text=text,
        meta=BlockMeta(order=order, page=page, heading_path=ancestors),
    )


def _paragraph(
    text: str, *, order: int, ancestors: tuple[str, ...] = (), page: int | None = None
) -> Block:
    return Block(
        type=BlockType.PARAGRAPH,
        text=text,
        meta=BlockMeta(order=order, page=page, heading_path=ancestors),
    )


def _long_paragraph(marker: str, *, order: int, sentences: int = 40) -> Block:
    text = "".join(
        f"這是{marker}的第{i}句話，內容足夠長以便撐開 token 預算。" for i in range(sentences)
    )
    return _paragraph(text, order=order)


class TestHeadingBoundaries:
    def test_a_new_heading_starts_a_new_chunk(self) -> None:
        doc = _doc(
            _heading("第一章", order=0),
            _paragraph("第一章的內容。", order=1, ancestors=("第一章",)),
            _heading("第二章", order=2),
            _paragraph("第二章的內容。", order=3, ancestors=("第二章",)),
        )

        chunks = chunk_document(doc)

        assert len(chunks) == 2
        assert "第一章的內容。" in chunks[0].text
        assert "第二章的內容。" in chunks[1].text
        assert "第二章" not in chunks[0].text

    def test_the_heading_travels_with_its_content(self) -> None:
        """標題不自成一塊——它單獨存在時沒有可檢索的資訊，卻會佔一個 chunk。"""
        doc = _doc(
            _heading("第一章 總則", order=0),
            _paragraph("本章說明適用範圍。", order=1, ancestors=("第一章 總則",)),
        )

        chunks = chunk_document(doc)

        assert len(chunks) == 1
        assert chunks[0].text.startswith("# 第一章 總則")
        assert "本章說明適用範圍。" in chunks[0].text

    def test_heading_path_is_carried_into_chunk_meta(self) -> None:
        doc = _doc(
            _heading("第一節", order=0, ancestors=("第一章",)),
            _paragraph("內容。", order=1, ancestors=("第一章", "第一節")),
        )

        chunks = chunk_document(doc)

        assert chunks[0].heading_path == ("第一章", "第一節")


class TestTables:
    def test_a_table_is_never_split(self) -> None:
        rows = "\n".join(f"| 項目{i} | 值{i} |" for i in range(200))
        table = Block(
            type=BlockType.TABLE,
            text=f"| 項目 | 數值 |\n| --- | --- |\n{rows}",
            meta=BlockMeta(order=0),
        )

        chunks = chunk_document(_doc(table))

        assert len(chunks) == 1
        assert "| 項目199 | 值199 |" in chunks[0].text

    def test_a_table_does_not_share_a_chunk_with_unrelated_prose(self) -> None:
        """表格自成一塊：混進段落時，檢索到的內容有一半與命中原因無關。"""
        doc = _doc(
            _paragraph("以下是規格：", order=0),
            Block(
                type=BlockType.TABLE,
                text="| a | b |\n| --- | --- |\n| 1 | 2 |",
                meta=BlockMeta(order=1),
            ),
        )

        chunks = chunk_document(doc)

        assert len(chunks) == 2
        assert "| 1 | 2 |" in chunks[1].text
        assert "| 1 | 2 |" not in chunks[0].text


class TestSizeLimits:
    def test_a_long_paragraph_is_split(self) -> None:
        config = ChunkConfig(target_tokens=120, overlap_tokens=20)

        chunks = chunk_document(_doc(_long_paragraph("甲", order=0)), config=config)

        assert len(chunks) > 1

    def test_every_chunk_respects_the_target(self) -> None:
        """每一塊都不得超過 target（表格與程式碼這類不可切的整塊除外）。

        超過的話，1C 的 embedding 呼叫會在**執行期**被 provider 以 token 上限退回，
        而那時文件已經走到一半——錯誤訊息指向 embedding，起因卻在這裡。
        """
        config = ChunkConfig(target_tokens=120, overlap_tokens=20)

        chunks = chunk_document(_doc(_long_paragraph("乙", order=0)), config=config)

        assert all(chunk.token_count <= config.target_tokens for chunk in chunks)

    def test_consecutive_chunks_overlap(self) -> None:
        """被迫從中間切開時要重疊，答案剛好落在切點上才不會兩塊都只有一半。"""
        config = ChunkConfig(target_tokens=120, overlap_tokens=40)

        chunks = chunk_document(_doc(_long_paragraph("丙", order=0)), config=config)

        assert len(chunks) > 1
        tail = chunks[0].text[-20:]
        assert tail in chunks[1].text

    def test_overlap_does_not_cross_a_heading(self) -> None:
        """重疊不跨節：把上一節的尾巴帶進下一節，等於在每一節開頭放一段不屬於它的話。"""
        config = ChunkConfig(target_tokens=200, overlap_tokens=40)
        doc = _doc(
            _heading("甲節", order=0),
            _paragraph("甲節的內容全部在這裡。", order=1, ancestors=("甲節",)),
            _heading("乙節", order=2),
            _paragraph("乙節的內容全部在這裡。", order=3, ancestors=("乙節",)),
        )

        chunks = chunk_document(doc, config=config)

        assert "甲節" not in chunks[1].text


class TestChunkShape:
    def test_sequence_numbers_start_at_zero_and_are_contiguous(self) -> None:
        """``seq`` 是 chunks 表的唯一鍵之一（05 §3.2 的 uq_chunk_document_version_seq）。"""
        config = ChunkConfig(target_tokens=120, overlap_tokens=20)

        chunks = chunk_document(_doc(_long_paragraph("丁", order=0)), config=config)

        assert [chunk.seq for chunk in chunks] == list(range(len(chunks)))

    def test_text_is_markdown(self) -> None:
        doc = _doc(
            _heading("標題", order=0),
            _paragraph("內容。", order=1, ancestors=("標題",)),
        )

        chunks = chunk_document(doc)

        assert chunks[0].text == "# 標題\n\n內容。"

    def test_page_comes_from_the_first_block_in_the_chunk(self) -> None:
        """引用要指得出頁——取塊內第一個 block 的頁碼（使用者從那裡開始讀）。"""
        doc = _doc(
            _heading("章", order=0, page=7),
            _paragraph("內容。", order=1, ancestors=("章",), page=7),
        )

        chunks = chunk_document(doc)

        assert chunks[0].page == 7

    def test_each_chunk_in_a_multi_page_section_keeps_its_own_page(self) -> None:
        """同一節橫跨多頁時，第 2..N 塊要帶**自己**第一個 block 的頁碼。

        全部沿用節首頁碼的話，內容在第 17 頁的引用會顯示「出自第 3 頁」，使用者
        翻到第 3 頁找不到——而所有多頁 section 都會靜默出錯。
        """
        config = ChunkConfig(target_tokens=40, overlap_tokens=0)

        def para(n: int) -> Block:
            return _paragraph(f"這是第{n}頁的內容。" * 4, order=n, ancestors=("章",), page=n)

        doc = _doc(_heading("章", order=0, page=3), para(3), para(4), para(5))

        chunks = chunk_document(doc, config=config)

        assert len(chunks) == 3
        assert [chunk.page for chunk in chunks] == [3, 4, 5]

    def test_split_pieces_of_one_block_keep_that_blocks_page(self) -> None:
        """單一長段落被切成多塊時，每一塊都還是那個 block 的頁碼。"""
        config = ChunkConfig(target_tokens=60, overlap_tokens=0)
        text = "這一段話非常長，必須被切成好幾塊才裝得下。" * 20

        chunks = chunk_document(_doc(_paragraph(text, order=0, page=9)), config=config)

        assert len(chunks) > 1
        assert all(chunk.page == 9 for chunk in chunks)

    def test_token_count_is_recorded(self) -> None:
        """token 數進 chunk：08 §4 的 stats 要算平均，1C 的批次也要靠它控制大小。"""
        chunks = chunk_document(_doc(_paragraph("一段內容。", order=0)))

        assert chunks[0].token_count > 0

    def test_an_empty_document_produces_no_chunks(self) -> None:
        assert chunk_document(_doc()) == []

    def test_a_heading_with_no_content_is_dropped(self) -> None:
        """只有標題、沒有內容的節不產生 chunk——那一塊檢索到也沒有東西可回答。"""
        doc = _doc(_heading("空的一節", order=0))

        assert chunk_document(doc) == []


class TestSeparatorPreservation:
    """切句不得丟掉句間的空白／換行。

    入庫文字與原文不符時三處一起壞：FTS 對 fused token 比對失敗、引用 snippet
    顯示黏字、LLM 收到黏字輸入。
    """

    def test_spaces_between_western_sentences_survive_a_split(self) -> None:
        config = ChunkConfig(target_tokens=60, overlap_tokens=0)
        text = " ".join(f"Sentence number {i} ends right here." for i in range(30))

        chunks = chunk_document(_doc(_paragraph(text, order=0)), config=config)

        assert len(chunks) > 1
        joined = "\n".join(chunk.text for chunk in chunks)
        assert "here.Sentence" not in joined
        # 每一句都完整存在於某一塊（切點只落在句子之間，不丟字也不黏字）。
        for i in range(30):
            assert f"Sentence number {i} ends right here." in joined

    def test_newlines_inside_a_paragraph_survive_a_split(self) -> None:
        config = ChunkConfig(target_tokens=60, overlap_tokens=0)
        text = "\n".join(f"alpha{i} beta{i}." for i in range(40))

        chunks = chunk_document(_doc(_paragraph(text, order=0)), config=config)

        assert len(chunks) > 1
        for chunk in chunks:
            assert not re.search(r"\.alpha", chunk.text)
        assert "beta0.\nalpha1" in chunks[0].text

    def test_overlap_carry_keeps_sentence_spacing(self) -> None:
        """重疊帶到下一塊的尾巴，句與句之間的空白也要保留。"""
        config = ChunkConfig(target_tokens=60, overlap_tokens=25)
        text = " ".join(f"Sentence number {i} ends right here." for i in range(30))

        chunks = chunk_document(_doc(_paragraph(text, order=0)), config=config)

        assert len(chunks) > 1
        for chunk in chunks:
            assert "here.Sentence" not in chunk.text

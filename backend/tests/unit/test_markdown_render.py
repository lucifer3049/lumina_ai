"""驗收：`ExtractedDoc` → Markdown 渲染（1B-4 收尾）。

**為什麼渲染而不是讓 loader 直接產 Markdown**：純 Markdown 是一串字，頁碼與 block
型別在裡面不存在。1D 的引用要說「出自第 12 頁〈第三章〉」——那個資訊只活在 blocks
的 meta 裡。所以中間格式仍是 `ExtractedDoc`，Markdown 是**下游要餵進 LLM 時的序列化
形式**：chunk 存的是 Markdown，chunk 的 meta 仍帶 page 與 heading_path，兩邊都拿得到。

這一層要驗的是「結構有沒有翻譯對」：

1. 標題深度 → ``#`` 的數量。深度錯的話，LLM 讀到的階層與文件不同，摘要會把子節
   當成獨立主題。
2. 表格維持表格形狀。攤平成一行數字正是使用者說「PDF 切出來超級不準確」的那個症狀。
3. 段落之間空行分隔——Markdown 少了空行，兩段會被算成同一段。
4. 原文不被竄改：內容本身是使用者的文件，渲染器只加結構、不改字。
"""

from __future__ import annotations

from etl.extract.markdown import to_markdown
from etl.extract.model import Block, BlockMeta, BlockType, ExtractedDoc


def _doc(*blocks: Block) -> ExtractedDoc:
    return ExtractedDoc(blocks=tuple(blocks), doc_meta={"media_type": "text/plain"})


def _heading(text: str, *, order: int, ancestors: tuple[str, ...] = ()) -> Block:
    return Block(
        type=BlockType.HEADING,
        text=text,
        meta=BlockMeta(order=order, heading_path=ancestors),
    )


def _paragraph(text: str, *, order: int, ancestors: tuple[str, ...] = ()) -> Block:
    return Block(
        type=BlockType.PARAGRAPH,
        text=text,
        meta=BlockMeta(order=order, heading_path=ancestors),
    )


class TestHeadings:
    def test_depth_becomes_hash_count(self) -> None:
        """標題深度由 ``heading_path`` 的長度決定（那是它的祖先數）。

        loader 不記「第幾層」而記祖先路徑，是因為來源之間的層級語意不同：docx 的
        ``Heading 2`` 在一份沒有 ``Heading 1`` 的文件裡實際上是最外層。用祖先數推
        導出來的深度永遠與文件自身的結構一致。
        """
        doc = _doc(
            _heading("第一章", order=0),
            _heading("第一節", order=1, ancestors=("第一章",)),
        )

        assert to_markdown(doc) == "# 第一章\n\n## 第一節"

    def test_depth_is_capped_at_six(self) -> None:
        """Markdown 只到 ``######``。更深的層級不能繼續加井字號。

        七個井字號不是標題，是一段以 ``#`` 開頭的普通文字——階層會在那一層突然
        塌掉，而輸出看起來仍然「有東西」，不會有人發現。
        """
        deep = tuple(f"層{i}" for i in range(8))
        doc = _doc(_heading("最深", order=0, ancestors=deep))

        assert to_markdown(doc).startswith("###### 最深")


class TestParagraphs:
    def test_blocks_are_separated_by_a_blank_line(self) -> None:
        """段落之間要空行——少了它，Markdown 會把兩段算成同一段。"""
        doc = _doc(_paragraph("第一段", order=0), _paragraph("第二段", order=1))

        assert to_markdown(doc) == "第一段\n\n第二段"

    def test_text_is_not_rewritten(self) -> None:
        """渲染器只加結構，不動內容。

        內容是使用者的文件。這裡若做了「聰明」的正規化（全形轉半形、去除多餘空白），
        引用回原文時會對不上，而差異小到不會有人在測試裡發現。
        """
        original = "價格是 $100 * 2，見 http://example.com/a_b_c"
        doc = _doc(_paragraph(original, order=0))

        assert to_markdown(doc) == original


class TestTables:
    def test_table_block_keeps_its_grid(self) -> None:
        """表格 block 原樣輸出（loader 已寫成 GFM 表格）。

        表格的格線在 loader 那一層才知道——只有它看得到儲存格的行列。渲染器再從
        一串攤平的文字猜回欄位邊界的話，含分隔符的儲存格會把整張表切錯。
        """
        table = Block(
            type=BlockType.TABLE,
            text="| 項目 | 數值 |\n| --- | --- |\n| 延遲 | 300ms |",
            meta=BlockMeta(order=0),
        )
        doc = _doc(_heading("規格", order=0), table)

        assert to_markdown(doc) == "# 規格\n\n| 項目 | 數值 |\n| --- | --- |\n| 延遲 | 300ms |"


class TestOtherBlockTypes:
    def test_code_is_fenced(self) -> None:
        """程式碼要包在 fence 裡：沒有 fence 的話，其中的 ``#`` 與 ``|`` 會被當成標記。"""
        doc = _doc(Block(type=BlockType.CODE, text="print('hi')", meta=BlockMeta(order=0)))

        assert to_markdown(doc) == "```\nprint('hi')\n```"

    def test_empty_document_renders_as_empty_string(self) -> None:
        """沒有 block 就是空字串，不是 ``None``——下游會直接把它接進 prompt。"""
        assert to_markdown(_doc()) == ""

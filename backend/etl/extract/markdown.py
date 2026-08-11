"""`ExtractedDoc` → Markdown。

**Markdown 是序列化形式，不是中間格式。** 中間格式仍是 `ExtractedDoc`：純 Markdown
是一串字，頁碼與 block 型別在裡面不存在，而 1D 的引用要說得出「出自第 12 頁〈第三
章〉」。所以 blocks 保留全部結構，這裡只在「要餵給 LLM」的那一刻把它寫成 Markdown。

**為什麼餵 Markdown 而不是純文字**：標題與表格在純文字裡消失得無聲無息——一張表被
攤成「2025 3.2 通過」之後，模型無從知道哪個數字屬於哪一欄，答案於是看起來很有自信
地錯。Markdown 是 LLM 訓練資料裡最常見的結構標記，成本只有幾個符號。

渲染器**不改內容**：正規化（全形轉半形、壓縮空白）會讓引用回原文時對不上，而差異
小到不會有人在測試裡發現。
"""

from __future__ import annotations

from etl.extract.model import Block, BlockType, ExtractedDoc

# Markdown 標題最深六層。第七層寫成 ``#######`` 不是標題，而是一段以井字號開頭的
# 普通文字——階層會在那裡塌掉，輸出卻仍然「看起來有東西」。
_MAX_HEADING_DEPTH = 6

_BLOCK_SEPARATOR = "\n\n"


def to_markdown(doc: ExtractedDoc) -> str:
    """整份文件的 Markdown。空文件回空字串（下游會直接接進 prompt）。"""
    return _BLOCK_SEPARATOR.join(block_to_markdown(block) for block in doc.blocks)


def block_to_markdown(block: Block) -> str:
    """單一 block 的 Markdown。

    切塊（1B-5）是以 block 為單位組合的，因此渲染必須能只渲染一塊——否則 chunk 的
    內容會與整份文件的渲染結果不一致，而引用比對就是靠那個字串。
    """
    match block.type:
        case BlockType.HEADING:
            # 深度來自祖先數，不是 loader 自報的層級：來源之間的層級語意不同
            # （沒有 Heading 1 的 docx 裡，Heading 2 實際上就是最外層）。
            depth = min(len(block.meta.heading_path) + 1, _MAX_HEADING_DEPTH)
            return f"{'#' * depth} {block.text}"
        case BlockType.TABLE:
            # 表格已是 GFM：只有 loader 看得到儲存格的行列邊界。在這裡從一串攤平的
            # 文字猜回欄位，含分隔符的儲存格會把整張表切錯。
            return block.text
        case BlockType.CODE:
            return f"```\n{block.text}\n```"
        case BlockType.CAPTION:
            return f"*{block.text}*"
        case _:
            return block.text


def rows_to_markdown_table(rows: list[list[str]]) -> str:
    """列 → GFM 表格。**供 loader 使用**（表格的形狀只有它知道）。

    第一列當表頭：表頭決定每一欄的意義，少了它「2025 | 3.2 | 通過」對模型毫無意義。
    儲存格裡的 ``|`` 要跳脫，否則一格會被讀成兩格，整列從那裡開始全部錯位。
    """
    if not rows:
        return ""

    width = max(len(row) for row in rows)
    padded = [[_escape_cell(cell) for cell in row] + [""] * (width - len(row)) for row in rows]

    header, *body = padded
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _escape_cell(cell: str) -> str:
    """儲存格內的換行也要處理：GFM 的表格列不能跨行，多行儲存格會把表格截斷。"""
    return cell.replace("|", "\\|").replace("\n", "<br>").strip()

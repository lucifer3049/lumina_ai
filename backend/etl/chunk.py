"""Recursive chunker（06 §2.1、08 §4）。

**chunk 同時是檢索的單位與引用的單位**，因此要滿足兩件互相拉扯的事：夠大（含得下
判斷語意所需的上下文）、夠小（塞得進 token 預算，命中時才精準）。

策略：結構感知的遞迴切塊。

1. **標題邊界優先**。跨節的兩段話放進同一塊，檢索命中後 LLM 拿到兩個主題混在一起的
   內容，沒有辦法分辨哪一半才是答案。
2. **不可切的整塊**（表格、程式碼）自成一塊。表格切一半之後，後半沒有表頭，每一列
   都失去欄位意義；而那正是「PDF 切出來超級不準確」的典型症狀。
3. **超長段落才往下遞迴**（段落 → 句子 → 硬切）。先試大的邊界、切不動才降級——這是
   這個策略叫 recursive 的原因。
4. **重疊**只發生在「被迫從中間切開」時，且**不跨標題**。跨節的重疊等於在每一節開頭
   放一段不屬於它的話。

chunk 的文字是 **Markdown**（`etl.extract.markdown` 的序列化），meta 帶頁碼與
heading_path——1D 的引用要指得出「第幾頁、哪一節」。

token 計數可注入（`count_tokens`）：真正的切詞由模型決定，而模型屬 AI Gateway（1C）。
預設用 `etl.tokens.estimate_tokens` 的保守估計，見該模組的說明。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from etl.extract.markdown import block_to_markdown
from etl.extract.model import Block, BlockType, ExtractedDoc
from etl.tokens import TokenCounter, estimate_tokens

# block 之間的 Markdown 分隔（同 `etl.extract.markdown`）。
_SEPARATOR = "\n\n"

# 句子邊界：中文句號驚嘆問號分號、西文句點問號驚嘆號後接空白、以及換行。
# 標點**留在前一句**（lookbehind），否則切完的句子開頭會是一個孤零零的句號。
# 整個 pattern 包成 capture group：`re.split` 才會把分隔的空白／換行一併回傳，
# 讓 `_sentences` 併回前一句。丟掉分隔符的話，重組出的 chunk 文字會與原文不符
# （西文句子黏成 `story.The`、硬換行黏成 `oneline`），FTS 與引用 snippet 一起壞。
_SENTENCE_BOUNDARY = re.compile(r"((?<=[。！？；!?;])\s*|(?<=[.!?])\s+|\n+)")

# 不可切開的 block 型別。切開的代價（表頭消失、程式碼半截）遠大於超出 target。
_ATOMIC = {BlockType.TABLE, BlockType.CODE}


@dataclass(frozen=True, slots=True)
class ChunkConfig:
    """切塊參數（06 §2.1）。

    **這裡的預設值只是型別上的方便，不是「系統預設值」。** 正式路徑一律由
    `services/knowledge/ingestion.py` 從 `app_settings` 帶入，再讓 KB config 覆寫
    （15 §4.1：可調參數集中在單一來源）。留一份在這裡是為了讓 `etl/` 不必 import
    設定——它是內層，而測試也需要一個「隨便給個合理值」的建構方式。
    """

    target_tokens: int = 512
    overlap_tokens: int = 64


@dataclass(frozen=True, slots=True)
class TextChunk:
    """一個 chunk。對映 `apps.knowledge.models.Chunk` 的 content / token_count / meta。"""

    seq: int
    text: str
    token_count: int
    page: int | None
    heading_path: tuple[str, ...]


def chunk_document(
    doc: ExtractedDoc,
    *,
    config: ChunkConfig | None = None,
    count_tokens: TokenCounter = estimate_tokens,
) -> list[TextChunk]:
    """把 `ExtractedDoc` 切成 chunk。空文件回空 list。"""
    settings = config or ChunkConfig()
    builder = _Builder(settings, count_tokens)

    for block in doc.blocks:
        if block.type is BlockType.HEADING:
            # 標題是邊界，同時是下一塊的前綴——它單獨存在沒有可檢索的資訊，
            # 卻能讓底下每一塊都說得出自己屬於哪一節。
            builder.start_section(block)
            continue
        if block.type in _ATOMIC:
            builder.add_atomic(block)
            continue
        builder.add_text(block)

    return builder.finish()


class _Builder:
    """累積 block、在超過 target 或遇到標題時切出 chunk。"""

    def __init__(self, config: ChunkConfig, count_tokens: TokenCounter) -> None:
        self._config = config
        self._count = count_tokens
        self._chunks: list[TextChunk] = []
        # 目前這一節的標題（Markdown 形式）與它的 heading_path／頁碼。
        self._heading_markdown: str | None = None
        self._heading_path: tuple[str, ...] = ()
        self._parts: list[str] = []
        self._page: int | None = None
        # 這一節內是否已經切出過 chunk——決定要不要帶重疊。
        self._carry: str = ""

    def start_section(self, block: Block) -> None:
        self._flush()
        self._carry = ""
        self._heading_markdown = block_to_markdown(block)
        self._heading_path = (*block.meta.heading_path, block.text)
        self._page = block.meta.page

    def add_atomic(self, block: Block) -> None:
        """表格／程式碼：自成一塊，不與其他內容共用 chunk。"""
        self._flush()
        self._carry = ""
        self._emit(block_to_markdown(block), page=block.meta.page)

    def add_text(self, block: Block) -> None:
        text = block_to_markdown(block)
        if not text.strip():
            return
        if self._page is None:
            self._page = block.meta.page

        if self._fits(self._joined([*self._parts, text])):
            self._parts.append(text)
            return

        # 裝不下：先把已累積的內容切出去，再處理這個 block 本身。
        self._flush()
        for piece in self._split_to_fit(text):
            if self._page is None:
                self._page = block.meta.page
            if self._fits(self._joined([*self._parts, piece])):
                self._parts.append(piece)
            else:
                self._flush()
                self._page = block.meta.page
                self._parts.append(piece)

    def finish(self) -> list[TextChunk]:
        self._flush()
        return self._chunks

    # ── 內部 ──────────────────────────────────────────────

    def _joined(self, parts: list[str]) -> str:
        prefix = [self._heading_markdown] if self._heading_markdown else []
        carry = [self._carry] if self._carry else []
        return _SEPARATOR.join([*prefix, *carry, *parts])

    def _fits(self, text: str) -> bool:
        return self._count(text) <= self._config.target_tokens

    def _flush(self) -> None:
        if not self._parts:
            return
        body = self._joined(self._parts)
        # 重疊只在同一節內延續：下一塊帶這一塊的尾巴，答案落在切點上時才不會兩塊
        # 都只有一半。跨節的重疊在 `start_section` 被清掉。
        self._carry = self._tail(_SEPARATOR.join(self._parts))
        self._parts = []
        self._emit(body, page=self._page)
        # 頁碼屬於「這一塊」：下一塊要從它自己的第一個 block 重新取。沿用的話，
        # 多頁 section 的第 2..N 塊全部指到節首頁，引用會叫使用者翻錯頁。
        self._page = None

    def _emit(self, text: str, *, page: int | None) -> None:
        body = text.strip()
        if not body:
            return
        self._chunks.append(
            TextChunk(
                seq=len(self._chunks),
                text=body,
                token_count=self._count(body),
                page=page,
                heading_path=self._heading_path,
            )
        )

    def _tail(self, text: str) -> str:
        """取尾端約 ``overlap_tokens`` 的句子作為下一塊的開頭。"""
        budget = self._config.overlap_tokens
        if budget <= 0:
            return ""
        picked: list[str] = []
        for sentence in reversed(_sentences(text)):
            candidate = [sentence, *picked]
            # 句子自帶尾端分隔符（見 `_sentences`），直接串接即可，計數也照串接算。
            if self._count("".join(candidate)) > budget:
                break
            picked = candidate
        return "".join(picked).strip()

    def _split_to_fit(self, text: str) -> list[str]:
        """把裝不下的文字遞迴切小：先按句子，句子還太長才硬切。

        硬切是最後手段（切在字中間會傷語意），但沒有它，一句沒有標點的超長文字會
        永遠裝不進任何 chunk——而那種輸入真的存在（掃描件的整段、程式碼貼進 Word）。
        """
        budget = self._budget_for_body()
        pieces: list[str] = []
        buffer = ""
        for sentence in _sentences(text):
            if self._count(buffer + sentence) <= budget:
                buffer += sentence
                continue
            if buffer:
                pieces.append(buffer)
                buffer = ""
            if self._count(sentence) <= budget:
                buffer = sentence
            else:
                pieces.extend(_hard_split(sentence, budget, self._count))
        if buffer:
            pieces.append(buffer)
        # 尾端分隔符到此功成身退：piece 之間改由 `_joined` 的 _SEPARATOR 銜接，
        # 留著只會變成「空白＋空行」的怪縫。piece **內部**的空白／換行原樣保留。
        return [piece.rstrip() for piece in pieces if piece.strip()]

    def _budget_for_body(self) -> int:
        """扣掉標題與重疊之後，正文還能用多少 token。

        不扣的話，加上前綴的成品會超出 target——而超出的那一塊要到 1C 呼叫 embedding
        時才會被 provider 退回，那時文件已經處理到一半。
        """
        overhead = self._count(self._joined([])) if (self._heading_markdown or self._carry) else 0
        return max(self._config.target_tokens - overhead, 1)


def _sentences(text: str) -> list[str]:
    """切句。分隔符（空白／換行）併回**前一句**的尾端——結果串接起來等於原文。

    `re.split` 帶 capture group 時會交錯回傳「句子、分隔符、句子…」；照原始順序
    把純空白的元素黏到前一個元素上，就不會丟任何字元。
    """
    parts: list[str] = []
    pending = ""
    for piece in _SENTENCE_BOUNDARY.split(text):
        if not piece:
            continue
        if piece.strip():
            parts.append(pending + piece)
            pending = ""
        elif parts:
            parts[-1] += piece
        else:
            # 開頭就是空白（例：文字以換行起始）：留給第一個句子當前綴。
            pending += piece
    if pending and parts:
        parts[-1] += pending
    elif pending:
        parts.append(pending)
    return parts or [text]


def _hard_split(text: str, budget: int, count: TokenCounter) -> list[str]:
    """按字元硬切。以 token 預算換算成字元步長，逐段確認不超標。"""
    if not text:
        return []
    ratio = max(count(text), 1) / len(text)
    step = max(int(budget / ratio), 1)
    return [text[start : start + step] for start in range(0, len(text), step)]

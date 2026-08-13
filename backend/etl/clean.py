"""Clean 階段（06 §2.1、08 §4）—— 丟掉不是內容的東西。

三件事，各自對應一種若不做就會污染整個知識庫的雜訊：

1. **跨頁重複的頁首頁尾**。每一頁都有的「XX 公司機密文件」不去掉，就會出現在幾乎
   每個 chunk 裡；它於是在每一次檢索都命中，而它從來不是答案。
2. **亂碼 block**。編碼壞掉或抽取失敗的殘骸照樣會被嵌入，佔用向量空間卻永遠不該被
   檢索到。丟棄率同時是**來源品質的訊號**——08 §4 要求 > 20% 示警，因為那通常代表
   這份來源需要人看一眼（掃描件、錯誤的編碼、加密的內文）。
3. **語言**。寫進 doc_meta 供 embedding 模型選擇與跨語言檢索觀測（06 §3.4）。判定用
   py3langid（BSD，離線模型），但**假名與諺文的字元證據排在模型之前**——漢字為主的
   日文在位元組 n-gram 模型下容易被判成中文，而有假名就一定是日文。

**不做的事同樣重要：不改寫使用者的文字。** 正規化只處理空白與零寬字元。全形轉半形、
標點統一這類「聰明」的處理會讓 1D 的引用對不回原文，而那種偏差小到不會有人發現。

Clean 是**純函式**：輸入 `ExtractedDoc`、輸出新的 `ExtractedDoc` 與統計。不落地、不
呼叫外部服務——這樣它才能被窮舉測試，而 08 §6 的重跑也不必擔心它有副作用。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import cache

from py3langid.langid import MODEL_FILE, LanguageIdentifier

from etl.extract.model import Block, BlockMeta, BlockType, ExtractedDoc
from etl.tokens import estimate_tokens

# 丟棄率超過這個比例即示警（08 §4）。
HIGH_DROP_RATE = 0.2

# 一個 block 裡「不可讀字元」的比例超過這個值就丟棄。0.3 是刻意寬鬆的：中文文件裡
# 偶爾出現的 � 是抽取的小瑕疵，整段丟掉的代價（少一段真內容）比留著大。
_GARBLED_RATIO = 0.3

# 頁首頁尾的候選長度上限。頁首必短；長段落重複出現通常是真的重複內容（條款、免責
# 聲明），那是使用者會問的東西，判成雜訊刪掉會讓他們得到「查無資料」。
#
# 以 **token** 而不是字元數當上限：同樣 40 個字元，中文是四十來個 token 的一整段話，
# 英文卻只是一行「Confidential — Internal Use Only」。用字元數的話，門檻對其中一種
# 語言必然是錯的。
_HEADER_MAX_TOKENS = 16

# 一段文字要出現在至少這麼多頁，才算頁首頁尾。兩頁太容易誤判（章節標題常跨頁重複），
# 三頁起才是「每一頁都有」的形狀。
_HEADER_MIN_PAGES = 3

# 零寬字元與 BOM：看不見，卻會讓同一句話的 hash 與字串比對全部對不上。
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"))

# 每行行尾的空白。行內空白不動——它可能是對齊，也可能是原文。
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)

# 三個以上的連續換行壓成兩個（= 一個空行）。更多的空行是排版產物，不是段落界線。
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")

# 假名與諺文是決定性證據，走在統計模型之前：漢字為主的日文（標題、法規條文）在
# 位元組 n-gram 模型下容易被判成中文，而那會讓 06 §3.4 的跨語言統計與 embedding
# 模型選擇一起錯。有假名就一定是日文，這種判斷不需要模型。
_KANA = re.compile(r"[぀-ヿ]")
_HANGUL = re.compile(r"[가-힯]")

# 低於這個信心度就回 und。py3langid 對任何輸入都會給一個答案——一行 "a" 也會得到
# 'en'（實測信心 0.17）。沒有門檻的話，封面頁或只有編號的文件會被貼上隨機語言標籤。
_MIN_CONFIDENCE = 0.5

# 語言判定的取樣上限。整份文件送進模型只是浪費 CPU——語言在前幾段就決定了，
# 而 ETL 的每一份文件都會走這一步。
_SAMPLE_CHARS = 2000


@dataclass(frozen=True, slots=True)
class CleanStats:
    """08 §4 要求落進 ``etl_jobs.stats`` 的品質數字。"""

    total_blocks: int
    dropped_blocks: int
    removed_headers: int
    language: str

    @property
    def drop_rate(self) -> float:
        return self.dropped_blocks / self.total_blocks if self.total_blocks else 0.0

    @property
    def quality_warning(self) -> bool:
        """丟棄率過高 → 通知使用者檢查來源，而不是安靜產出半份知識庫。"""
        return self.drop_rate > HIGH_DROP_RATE


def clean(doc: ExtractedDoc) -> tuple[ExtractedDoc, CleanStats]:
    """去噪與正規化。回傳新的文件與統計，原文件不變。"""
    headers = _repeated_page_texts(doc)

    kept: list[Block] = []
    dropped = 0
    removed_headers = 0

    for block in doc.blocks:
        if _normalize_for_match(block.text) in headers:
            removed_headers += 1
            continue

        text = _normalize(block.text) if block.type is not BlockType.TABLE else block.text
        if not text.strip():
            dropped += 1
            continue

        if block.type is not BlockType.TABLE and _is_garbled(text):
            dropped += 1
            continue

        kept.append(
            Block(
                type=block.type,
                text=text,
                # order 重新編號：留著洞的話，下游「這是第幾個 block」的推算會與實際
                # 位置不一致，而那種偏差不報錯，只讓鄰接關係悄悄錯位。
                meta=BlockMeta(
                    order=len(kept),
                    page=block.meta.page,
                    heading_path=block.meta.heading_path,
                ),
            )
        )

    language = _detect_language(kept)
    stats = CleanStats(
        # 分母是**進來時**的 block 數：丟棄率要回答「這份來源有多少讀不出來」。
        total_blocks=len(doc.blocks) - removed_headers,
        dropped_blocks=dropped,
        removed_headers=removed_headers,
        language=language,
    )
    doc_meta = {**doc.doc_meta, "language": language, "block_count": len(kept)}
    return ExtractedDoc(blocks=tuple(kept), doc_meta=doc_meta), stats


def _repeated_page_texts(doc: ExtractedDoc) -> set[str]:
    """找出跨頁重複的短文字（頁首頁尾）。

    沒有頁碼的來源（txt / Markdown / 試算表）直接回空集合：「跨頁重複」的前提是有頁，
    而純文字裡重複的短行是清單項或小標題——刪掉它們會把真正的內容挖掉。
    """
    pages_by_text: dict[str, set[int]] = {}
    for block in doc.blocks:
        if block.meta.page is None or block.type is BlockType.TABLE:
            continue
        text = _normalize_for_match(block.text)
        if not text or estimate_tokens(text) > _HEADER_MAX_TOKENS:
            continue
        pages_by_text.setdefault(text, set()).add(block.meta.page)

    return {text for text, pages in pages_by_text.items() if len(pages) >= _HEADER_MIN_PAGES}


def _normalize_for_match(text: str) -> str:
    """比對用：去零寬字元、壓縮所有空白。

    頁首每頁的空白可能因排版而不同（PDF 的字距由座標決定），不壓縮的話「看起來一樣」
    的兩行會被算成不同的文字，偵測整個失效。
    """
    return " ".join(text.translate(_INVISIBLE).split())


def _normalize(text: str) -> str:
    """空白正規化。**只動空白與零寬字元。**"""
    cleaned = text.translate(_INVISIBLE)
    cleaned = _TRAILING_SPACE.sub("", cleaned)
    cleaned = _EXCESS_BLANK_LINES.sub("\n\n", cleaned)
    return cleaned.strip()


def _is_garbled(text: str) -> bool:
    """不可讀字元佔比過高即視為亂碼。

    判定對象是 Unicode 的替代字元（``�``）與控制字元/未分配碼位——它們是解碼
    失敗留下的痕跡。不用「看不看得懂」這種語意判斷：那需要模型，而且會誤殺專有名詞
    與程式碼。
    """
    if not text:
        return True
    bad = sum(1 for char in text if char == "�" or unicodedata.category(char) in {"Cc", "Cn"})
    return bad / len(text) > _GARBLED_RATIO


@cache
def _identifier() -> LanguageIdentifier:
    """py3langid 的模型（惰性載入）。

    模型是一組 numpy 陣列，載入要時間也佔記憶體。放在模組載入時做的話，**每一個**
    import 到 `etl.clean` 的行程都要付這筆成本——包括只做抽取、根本不會呼叫這裡的
    子行程（見 `etl.extract.sandbox`），而那個行程還有記憶體上限。
    """
    return LanguageIdentifier.from_pickled_model(MODEL_FILE, norm_probs=True)


def _detect_language(blocks: list[Block]) -> str:
    """語言判定：書寫系統決定得了的先判，其餘交給統計模型。

    信心不足時回 ``und`` 而不是模型的首選。py3langid 對任何輸入都會給答案——只有
    一個字母的封面頁也會得到 `en`。06 §3.4 的跨語言統計依這個欄位分組，隨機的標籤
    會讓那份數據失效，而「不知道」是誠實且可分辨的值。
    """
    sample = " ".join(block.text for block in blocks)[:_SAMPLE_CHARS]
    if not sample.strip():
        return "und"
    if _KANA.search(sample):
        return "ja"
    if _HANGUL.search(sample):
        return "ko"

    language, confidence = _identifier().classify(sample)
    return str(language) if float(confidence) >= _MIN_CONFIDENCE else "und"

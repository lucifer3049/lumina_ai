"""全文檢索的**純邏輯**（06 §3.1 的 FTS 一路、05 §5.3、13 §4 工作包 2B-1）。

與 `vector.py` 對稱：這一層不碰 ORM、不認識上層（鐵則 2），真正的 SQL 在
`ChunkRepository.search_fts`。回傳形狀也共用——FTS 的命中同樣經 `vector.to_retrieved`
變成 `RetrievedChunk`，因為 2B-2 要把兩路的候選混在一起融合，形狀不同的話融合那一層
就得為每一路各寫一次「這筆是從哪來的」。

**送給 PGroonga 的是整句原文，不是關鍵字清單**，理由是 2B-1 開工前的實測（暫存表，
2026-08-23）：

- `content &@ '<整句中文問句>'` → **0 筆**。PGroonga 把整句斷成 bigram 後**全部 AND**，
  而一段話不可能同時包含問句的每一個字組。
- `content &@~ '<整句>'` → 同樣 0 筆，而且 `(`、`-`、`"` 是查詢語法的運算子：問句裡
  一個括號就讓結果變空，**而且不報錯**。
- `content &@~ '密碼 OR 鎖住'` → 有效，但要我們自己把中文斷成詞。這個 build 沒有
  `pgroonga_tokenize`，Python 這側也沒有斷詞器——這條路現在走不通。
- `content &@* '<整句>'`（similar search）→ **正中**。它自己從查詢文字裡挑代表性的詞，
  短詞、英文問句、含語法字元的問句都正常，不相關的查詢與空白回空。

因此這裡**只做三件事**：去頭尾空白、把空查詢變成空字串（由呼叫端擋掉）、過長截斷。
**不做跳脫**——`&@*` 把那些字元當普通文字，補上 escape 只會讓查詢字串多出字面上的
反斜線，而那些反斜線會被當成要比對的字元。症狀是「問句裡有括號的那幾題突然都查不到」，
一樣沒有錯誤。

`&@~`（查詢語法）沒有被否定：2B-2 之後若要支援「必須包含某詞」的進階查詢，那是唯一
走得通的路。現在不做。
"""

from __future__ import annotations

__all__ = ["MAX_FTS_QUERY_CHARS", "normalise_fts_query"]

# **保護 DB 的硬上限，不是使用者可調的東西**（同 `services/rag/params.py` 的 `MAX_TOP_K`，
# 15 §4.1 的例外條款）。similar search 的成本隨查詢長度上升，而查詢字串的來源是使用者
# 輸入（`MessageCreateIn.content` 上限 4,000 字）——截在這裡，DB 那側才有上界。
#
# 1,000 字遠大於任何真實問句：它擋的是貼上整篇文章那種用法，不是正常提問。
MAX_FTS_QUERY_CHARS = 1000


def normalise_fts_query(query: str) -> str:
    """問句 → 送進 `&@*` 的字串。

    **空白查詢在這裡變成空字串**，由呼叫端拒絕（`search_fts` 會 raise）。不能讓它往下
    走：PGroonga 對空字串回空集合，而那與「這個知識庫真的沒有相關內容」在結果上一模
    一樣——兩者對上層的處置完全不同。
    """
    return query.strip()[:MAX_FTS_QUERY_CHARS]

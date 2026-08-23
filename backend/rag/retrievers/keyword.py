"""全文檢索的**查詢建構**（06 §3.1 的 FTS 一路、05 §5.3、13 §4 工作包 2B-1／2B-2b）。

與 `vector.py` 對稱：這一層不碰 ORM、不認識上層（鐵則 2），SQL 在
`ChunkRepository.search_fts`。回傳形狀共用——FTS 的命中同樣經 `vector.to_retrieved`
變成 `RetrievedChunk`，因為 RRF 要把兩路的候選混在一起。

## 為什麼是「識別符才發言」（2B-2b，2026-08-23）

2B-1 送整句問句進 `&@*`（similar search）。2B-2 的評測顯示那樣**反而更差**
（手寫題 recall@1 0.4375 → 0.3958、DRCD 0.9417 → 0.9333），追因後的實測很清楚：

- `ef_search` 單獨查 → 正解在前三名。
- 同一個詞放進 `「向量索引的 ef_search 建議從多少開始調？」` → **0 筆**。中文的 bigram
  把那個詞稀釋掉了，而 similar search 要求足夠多的代表詞同時命中。
- 換 bigram-OR（把整句切成字組再 OR）更糟：pgroonga 的分數沒有 IDF，比的是命中**次數**，
  於是長段落靠字數贏，正解掉出前五名。

**結論：字面檢索只有在問句帶著「向量最弱的那種詞」時才有話講。** 那種詞是識別符——
`ef_search`、`ES256`、`pgBackRest`、`PITR`、`第 14 條` 的 `14`。純中文的概念問句
（「租戶隔離怎麼做？」）與純小寫的英文問句沒有這種詞，**這一層就回空字串讓那一路棄權**：
沒有話講的時候閉嘴，比投一張模糊票好——後者的代價是把向量找對的答案擠下去。

判斷規則刻意保守（寧可棄權也不要亂投）：
- 含數字或 `_` `.` `-` → 是（`ES256`、`ef_search`、`v1.2`、`14`）
- 全大寫且長度 ≥2 → 是（`API`、`JWT`、`RRF`、`PITR`）
- 第一個字元之後還有大寫 → 是（`pgBackRest`、`MinIO`）
- 其餘（`why`、`Why`、`document`）→ 不是。句首大寫的普通英文單字會被這條擋掉。

送出的運算式是 `&@~` 的查詢語法（`"詞" OR "詞"`），每個詞用雙引號包住——詞本身只可能
由 `[A-Za-z0-9_.-]` 組成，包起來之後 `-` 之類的字元不會被當成運算子。
"""

from __future__ import annotations

import re

__all__ = ["MAX_FTS_QUERY_CHARS", "MAX_FTS_TERMS", "build_fts_query"]

# **保護 DB 的硬上限，不是使用者可調的東西**（同 `services/rag/params.py` 的 `MAX_TOP_K`，
# 15 §4.1 的例外條款）。查詢字串的來源是使用者輸入（`MessageCreateIn.content` 上限
# 4,000 字），截在這裡 DB 那側才有上界。
MAX_FTS_QUERY_CHARS = 1000
# 一句話裡的識別符很少超過個位數；上限擋的是「貼一整段設定檔進來提問」那種用法——
# 那會組出上百個 OR，而每個都要掃一次倒排索引。
MAX_FTS_TERMS = 12

# 詞的形狀：英數起頭的識別符，或純數字（`第 14 條` 的 `14`）。單一數字不算——
# 「3 天」的 `3` 到處都是。
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]+|[0-9]{2,}")


def build_fts_query(text: str) -> str:
    """問句 → `&@~` 的查詢運算式；**沒有識別符就回空字串（那一路棄權）**。

    回空字串不是失敗，是「這句話裡沒有字面比對幫得上忙的東西」。呼叫端
    （`services/rag/retrieval.py`）看到空字串就不打 DB——連查詢都不必送。
    """
    terms: list[str] = []
    for token in _TOKEN.findall(text[:MAX_FTS_QUERY_CHARS]):
        if _is_distinctive(token) and token not in terms:
            terms.append(token)
        if len(terms) >= MAX_FTS_TERMS:
            break
    return " OR ".join(f'"{term}"' for term in terms)


def _is_distinctive(token: str) -> bool:
    """這個詞是不是「向量最弱、字面最強」的那一種（見模組 docstring 的規則）。"""
    if any(char.isdigit() for char in token) or any(char in "_.-" for char in token):
        return True
    if token.isupper() and len(token) >= 2:
        return True
    return any(char.isupper() for char in token[1:])

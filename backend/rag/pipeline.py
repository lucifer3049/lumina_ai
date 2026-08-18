"""檢索編排的**純邏輯**（06 §3、02 §2 的 `rag/pipeline.py`，1D-5）。

三件事，都是「換了資料來源也不會變」的那一類：查詢字串怎麼組、多路候選怎麼併、
哪幾段進得了 context。SQL 在 `repositories/`、編排在 `services/rag/retrieval.py`
——這一層不碰 ORM 也不認識上層（鐵則 2）。

**這裡沒有任何預設值**：數字全由呼叫端從 `services/rag/params.py` 解析後傳進來
（15 §4.1 的可調參數集中）。在這裡放一份預設值等於多一個「後台改了沒有反應」的
藏身處。

現在只有一條路（純向量），所以看起來很薄。它存在是因為 Phase 2 的 hybrid 要在
`merge_candidates` 這個**同一個位置**把兩路候選以 RRF 融合，而 rerank 接在它與
`select_context` 之間；那時呼叫端一行都不必動。
"""

from __future__ import annotations

from collections.abc import Sequence

from etl.tokens import estimate_tokens
from rag.retrievers.vector import RetrievedChunk, normalise_query

__all__ = ["build_search_query", "merge_candidates", "select_context"]


def build_search_query(
    question: str, *, previous_questions: Sequence[str], history_turns: int
) -> str:
    """檢索要用的查詢字串——06 §3.1 的 condense 的**免錢版**（1D-5 決定）。

    文件的做法是「用小模型把指代性問句改寫成獨立問句」，那是每一輪多一次 LLM 呼叫。
    這裡做零成本的版本：**把前 N 個問題接上去一起查**。「那病假呢？」單獨拿去搜，
    命中的是一組與請假無關的內容——而模型會很有禮貌地依據那些內容回答。接上前一問
    之後，命中的是請假規定。

    **只影響檢索，不影響送給模型的問題**：模型那邊本來就看得到對話歷史（06 §5 的
    記憶視窗），把改寫過的問句餵給它反而會讓回答重複前一輪的內容。

    真正的 condense 排 Phase 2/3C——那時有 golden set，量得出它比這個好多少。
    """
    current = normalise_query(question)
    if history_turns <= 0:
        return current

    # 取**最近的** N 個。取最早的話，長對話裡檢索看到的永遠是開場白。
    recent = [normalise_query(item) for item in list(previous_questions)[-history_turns:]]
    return "\n".join([*(item for item in recent if item), current]).strip()


def merge_candidates(groups: Sequence[Sequence[RetrievedChunk]]) -> list[RetrievedChunk]:
    """多路候選 → 一份依相關性排好的清單。

    **跨 KB 之後只有分數是共同尺度。** 一場對話可以掛多個 KB（05 §3.4 的 `kb_ids`），
    每個 KB 各自查一次、各自由高到低排好；照 KB 順序串起來的話，第二個 KB 裡分數最高
    的那一段會排在第一個 KB 裡分數最低的那一段後面——而兩邊各自看起來都是排好的。

    去重是為了 Phase 2：現在 chunk 只屬於一個 KB，但同一份文件掛進兩個 KB（09 §2.2
    的未來項）與 hybrid（同一段同時被向量與 FTS 命中）都會讓同一段出現兩次，而重複的
    代價是**兩份 token 換零份新資訊**。

    RRF 融合（06 §3.1）接在這裡：那時排序依據從「分數」換成「兩路的名次倒數和」，
    而呼叫端看到的仍然是一份排好的清單。
    """
    seen: set[object] = set()
    merged: list[RetrievedChunk] = []
    for group in groups:
        for chunk in group:
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            merged.append(chunk)
    # `-score` 由高到低；`str(chunk_id)` 是穩定的第二鍵——同分時沒有它，兩次查詢
    # 排出來的順序可以不同，而那會讓引用編號在重跑時對不上。
    merged.sort(key=lambda chunk: (-chunk.score, str(chunk.chunk_id)))
    return merged


def select_context(
    candidates: Sequence[RetrievedChunk],
    *,
    max_chunks: int,
    token_budget: int,
    min_score_ratio: float = 0.0,
) -> list[RetrievedChunk]:
    """候選 → 真正進 context 的那幾段（06 §3.1 的 top_n、§3.2 的預算）。

    **檢索回來的東西不能整包丟給 LLM。** 候選是 40 段，而 context 預算約 4,500
    token：塞進去只有兩種結果，provider 以 context window 超限退回（看得見），或
    前面的指令被擠掉而模型開始自由發揮（看不見）。

    裁切一律從**低分端**下手。反過來的話留下的是最不相關的那幾段，而回答看起來只是
    「答得不好」，沒有任何地方指向裁切。

    `token_budget` 用 `estimate_tokens` 量，與 chunker 是**同一個函式**：chunk 的大小
    就是它量出來的，兩邊估法不同時這裡的算術對不起來，而症狀是偶爾超限。
    """
    kept = _apply_score_gate(candidates, min_score_ratio)

    selected: list[RetrievedChunk] = []
    used = 0
    for chunk in kept[:max_chunks]:
        cost = estimate_tokens(chunk.content)
        if selected and used + cost > token_budget:
            break
        selected.append(chunk)
        used += cost
    return selected


def _apply_score_gate(
    candidates: Sequence[RetrievedChunk], min_score_ratio: float
) -> list[RetrievedChunk]:
    """相對門檻：只留下分數 ≥ 第一名 × ratio 的候選（1D-5 決定，預設關閉）。

    **為什麼不是 06 §3.1 的絕對門檻 0.3**：那是 cross-encoder（rerank）的分數尺度，
    而 Phase 1 只有餘弦相似度。套上去的結果不是「品質變好」，是每次都回「知識庫中
    找不到相關內容」。相對門檻只比較「跟第一名差多少」，因此不吃尺度——2B 換上
    `bge-reranker-v2-m3` 之後它照樣有效，而那時絕對門檻才第一次有意義。

    兩個保護：第一名對自己的比值永遠是 1，所以 ratio 調到 1.0 也砍不掉它（否則使用者
    會看到「這個知識庫突然什麼都答不出來」）；**第一名的分數是負的時候整個關掉**
    ——餘弦可以是負的，而負數的八成比原本**大**（-0.2 × 0.8 = -0.16 > -0.2），
    門檻一開就把第二名以後全砍光。
    """
    if not candidates or min_score_ratio <= 0:
        return list(candidates)

    best = candidates[0].score
    if best <= 0:
        return list(candidates)

    floor = best * min_score_ratio
    return [chunk for index, chunk in enumerate(candidates) if index == 0 or chunk.score >= floor]

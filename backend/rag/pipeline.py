"""檢索編排的**純邏輯**（06 §3、02 §2 的 `rag/pipeline.py`，1D-5）。

三件事，都是「換了資料來源也不會變」的那一類：查詢字串怎麼組、多路候選怎麼併、
哪幾段進得了 context。SQL 在 `repositories/`、編排在 `services/rag/retrieval.py`
——這一層不碰 ORM 也不認識上層（鐵則 2）。

**這裡沒有任何預設值**：數字全由呼叫端從 `services/rag/params.py` 解析後傳進來
（15 §4.1 的可調參數集中）。在這裡放一份預設值等於多一個「後台改了沒有反應」的
藏身處。

2B-2 起這裡有三段，順序不可對調：**逐路門檻 → RRF 融合 → 裁進 context**。

門檻在融合**之前**是刻意的（2B-2 改，原本在 `select_context` 內）：RRF 之後每一段的
分數都是名次倒數和（第 1 名 1/61、第 10 名 1/70），彼此的比值全部落在 0.87~1.0 之間
——相對門檻設 0.8 砍不掉任何東西，設 0.99 則把第三名以後全砍光，而兩種都沒有錯誤。
融合之前比較的還是各路自己的尺度（餘弦相似度、pgroonga 分數），語意與 1D-5 當初定的
一致。rerank（2B-3／2B-4）接在融合與裁切之間。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from etl.tokens import estimate_tokens
from rag.retrievers.vector import RetrievedChunk, normalise_query

__all__ = [
    "build_search_query",
    "fuse_candidates",
    "gate_by_absolute_score",
    "gate_by_score",
    "select_context",
]


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


def fuse_candidates(
    groups: Sequence[Sequence[RetrievedChunk]], *, k: int, limit: int
) -> list[RetrievedChunk]:
    """多路候選 → 一份依相關性排好的清單（06 §3.1 的 RRF，k 預設 60 → 取 24）。

    **只看名次，不看分數。** 兩路的分數不是同一個尺度：向量那路是餘弦相似度（0~1
    上下），FTS 那路是 pgroonga 的分數（實測可達六位數）。直接比大小、加權平均、或
    「先各自正規化再合併」，結果都由**哪一路的數字比較大**決定，而不是由相關性決定
    ——而那個錯誤看起來只是「答案偏向某一種問法」。

    每一路的第 r 名（r 從 1 起算）貢獻 ``1 / (k + r)``，同一段在多路都出現就相加。
    於是「兩路都覺得不錯」勝過「單路覺得很棒」，而任何一路換打分方式（2B-4 接上
    rerank、或換 embedding 模型）都不會讓融合失效——那正是 06 §3.1 選它而不選加權
    融合的理由（「免調權重、對分數尺度不敏感」）。

    ``k`` 是「名次差距要壓多平」的旋鈕：越大越看重「有多少路都提到它」，越小越信任
    各路自己的排序。``limit`` 是 06 §3.1 的「→ 24」：不裁的話，2B-4 的 rerank 要對
    80 段做 cross-encoder 推論，那是 11 §4 延遲預算的好幾倍。

    **跨 KB 也走這裡**：一場對話可以掛多個 KB（05 §3.4 的 `kb_ids`），每個 KB 的每一路
    各是一個 group。照 KB 順序串起來的話，第二個 KB 裡最相關的那一段會排在第一個 KB
    最不相關的那一段後面——而兩邊各自看起來都是排好的。

    **`groups` 的順序就是優先序**：同分時排在前面的那一路贏。呼叫端把向量放第一路
    （`services/rag/retrieval.py`），理由是 2B-2 的實測——兩路的第一名各得 1/61，同分
    在 hybrid 裡是**常態而不是例外**，而原本以 `chunk_id` 決勝等於擲骰子：24 題的手寫
    題組裡有 9 題的正確答案就這樣被擠下 1~2 名（recall@1 0.4375 → 0.3333）。

    **回傳的 `score` 換成融合分數**（越大越相關，尺度本身沒有意義）。要「這一段在各路
    原本的分數」的話，那屬於 2B-5 的 `rag_trace`。
    """
    scores: dict[object, float] = {}
    chunks: dict[object, RetrievedChunk] = {}
    # 決勝用：這一段在各路裡拿過的最好名次，以及那個名次出自第幾路。
    best: dict[object, tuple[int, int]] = {}

    for index, group in enumerate(groups):
        seen_in_group: set[object] = set()
        for rank, chunk in enumerate(group, start=1):
            key = chunk.chunk_id
            # 同一路裡重複出現的 chunk 只算第一次。DB 那側不該回重複，但真的回了的話
            # 該段會被灌成第一名，而沒有任何地方看得出來。
            if key in seen_in_group:
                continue
            seen_in_group.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            chunks.setdefault(key, chunk)
            best[key] = min(best.get(key, (rank, index)), (rank, index))

    # 依序：融合分數高的先；同分則名次好的先；再同分則排在前面那一路的先；最後才用
    # `chunk_id`（純粹為了決定性——沒有它，兩次查詢的引用編號可以對不上）。
    ordered = sorted(scores, key=lambda key: (-scores[key], best[key], str(key)))
    return [replace(chunks[key], score=scores[key]) for key in ordered[:limit]]


def gate_by_score(
    candidates: Sequence[RetrievedChunk], *, min_score_ratio: float
) -> list[RetrievedChunk]:
    """相對門檻：只留下分數 ≥ 第一名 × ratio 的候選（1D-5 決定，預設關閉）。

    **套在融合之前、逐路各自套用**（2B-2 改）：融合之後的分數是名次倒數和，彼此比值
    永遠接近 1，門檻不是砍不掉東西就是把大半砍光（見模組 docstring）。

    **為什麼不是 06 §3.1 的絕對門檻 0.3**：那是 cross-encoder（rerank）的分數尺度，而
    這裡是餘弦相似度與 pgroonga 分數。套上去的結果不是「品質變好」，是每次都回「知識庫
    中找不到相關內容」。相對門檻只比較「跟第一名差多少」，因此不吃尺度——2B-4 換上
    `bge-reranker-v2-m3` 之後它照樣有效，而那時絕對門檻才第一次有意義。

    兩個保護：第一名對自己的比值永遠是 1，所以 ratio 調到 1.0 也砍不掉它（否則使用者
    會看到「這個知識庫突然什麼都答不出來」）；**第一名的分數是負的時候整個關掉**——
    餘弦可以是負的，而負數的八成比原本**大**（-0.2 × 0.8 = -0.16 > -0.2），門檻一開就
    把第二名以後全砍光。
    """
    if not candidates or min_score_ratio <= 0:
        return list(candidates)

    best = candidates[0].score
    if best <= 0:
        return list(candidates)

    floor = best * min_score_ratio
    return [chunk for index, chunk in enumerate(candidates) if index == 0 or chunk.score >= floor]


def gate_by_absolute_score(
    candidates: Sequence[RetrievedChunk], *, threshold: float
) -> list[RetrievedChunk]:
    """絕對門檻：低於 `threshold` 的一律丟掉（06 §3.1 的 0.3，2B-3 第一次生效）。

    **與相對門檻是兩件事，位置也不同**：相對門檻在融合之前、比的是「跟同一路的第一名
    差多少」；這一條在 **rerank 之後**、比的是 cross-encoder 給的 0~1 分數。

    **它會回空清單，而那是刻意的**（06 §3.3 的幻覺防線一）：全部低於門檻代表「知識庫
    裡沒有能回答這個問題的東西」，那時該讓模型誠實說不知道，而不是拿一堆不相關的段落
    硬答——後者產生的是看起來有根據的錯誤答案。

    **只有在 rerank 真的跑過時才准套用。** 跳過 rerank 之後手上是 RRF 的融合分數
    （第一名 1/61 ≈ 0.016），拿 0.3 去比會把全部砍光——那正是 1D-5 拒絕在 Phase 1
    啟用它的同一個理由。位置的強制在 `services/rag/retrieval.py`，這裡只做算術。
    """
    if threshold <= 0:
        return list(candidates)
    return [chunk for chunk in candidates if chunk.score >= threshold]


def select_context(
    candidates: Sequence[RetrievedChunk],
    *,
    max_chunks: int,
    token_budget: int,
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
    selected: list[RetrievedChunk] = []
    used = 0
    for chunk in candidates[:max_chunks]:
        cost = estimate_tokens(chunk.content)
        if selected and used + cost > token_budget:
            break
        selected.append(chunk)
        used += cost
    return selected

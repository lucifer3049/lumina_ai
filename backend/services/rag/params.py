"""檢索參數的解析 —— **可調參數唯一的入口**（15 §4.1、06 §3.1，1D-5）。

2026-08-17 的產品決定：凡是「要由使用者決定」的數字，不准寫死在邏輯裡；預設值住在
`config/settings/app_settings.py` 的可調參數區，覆寫順序固定：

    系統預設（app_settings，env 可蓋）
      → 租戶設定（09 §2.6 的 `/settings`，**屬 2C，這一層還不存在**）
        → KB 覆寫（05 §3.2 的 `knowledge_base.config` 的 `retrieval` 區）

**這一層現在就要在，理由不是為了現在能調**：數字一旦散進 `RetrievalService` 與
`ChatService`，2C 做設定畫面時要逐檔翻才蒐得齊，而漏掉一個的症狀是「後台改了沒有
反應」。1D-5 之前已經有一個實例——`top_k=40` 同時寫在 `RetrievalService` 的常數與
`/rag/query` 的簽章上，兩份漂掉時「除錯 API 查得到、實際問答查不到」。

**寫入時驗證、讀取時容忍。** KB config 是使用者寫得到的 JSON（2C 之後更是），而這裡
在熱路徑上：壞值退回預設並記一筆，比讓那個 KB 從此問不了問題好——後者使用者看到的
只是「一直出錯」。真正的驗證屬 2C 的設定端點。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from config.logging import get_logger
from config.settings.app_settings import get_app_settings

logger = get_logger(__name__)

__all__ = ["MAX_TOP_K", "RagParams", "resolve_rag_params"]

# KB config 裡放檢索參數的區塊名。與 1B 的切塊參數（`config.chunk`）同一個慣例——
# 兩套命名的話，2C 的設定畫面要為每個功能各寫一次讀寫邏輯。
_SECTION = "retrieval"

# **保護 DB 的硬上限，不是使用者可調的東西。** `top_k` 直接進 SQL 的 LIMIT，而沒有
# 上限時一個 `top_k=1000000` 不會失敗，只會讓 pgvector 把整個 KB 掃出來排序——那幾秒
# 對**所有租戶**都很慢。因此它住在程式碼裡而不是設定裡（15 §4.1 的例外條款）。
MAX_TOP_K = 200
_MAX_CONTEXT_CHUNKS = 50
_MAX_TOKEN_BUDGET = 100_000
# RRF 的 k 沒有物理上限，但大到某個程度之後每一段的貢獻都趨近 1/k——名次完全失去意義，
# 而結果看起來只是「排序怪怪的」。
_MAX_RRF_K = 1000
_MAX_HISTORY_TURNS = 10


@dataclass(frozen=True, slots=True)
class RagParams:
    """一次檢索要用的全部參數。**上層只認得這個型別**，不知道值從哪一層來。"""

    top_k: int
    fts_top_k: int
    rrf_k: int
    hybrid_candidates: int
    retrieval_mode: str
    rerank_threshold: float
    context_chunks: int
    context_token_budget: int
    min_score_ratio: float
    query_history_turns: int


def resolve_rag_params(kb_config: Mapping[str, Any] | None) -> RagParams:
    """系統預設 + KB 覆寫 → `RagParams`。

    租戶層（09 §2.6）屬 2C：接上時在這裡多疊一層，呼叫端一行都不必動——那正是把
    解析集中在一個函式裡的目的。
    """
    settings = get_app_settings()
    raw = (kb_config or {}).get(_SECTION)
    section: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}

    return RagParams(
        top_k=_int(section, "top_k", settings.rag_top_k, low=1, high=MAX_TOP_K),
        fts_top_k=_int(section, "fts_top_k", settings.rag_fts_top_k, low=1, high=MAX_TOP_K),
        rrf_k=_int(section, "rrf_k", settings.rag_rrf_k, low=1, high=_MAX_RRF_K),
        hybrid_candidates=_int(
            section,
            "hybrid_candidates",
            settings.rag_hybrid_candidates,
            low=1,
            high=MAX_TOP_K,
        ),
        retrieval_mode=_mode(section, settings.rag_retrieval_mode),
        rerank_threshold=_float(
            section, "rerank_threshold", settings.rag_rerank_threshold, low=0.0, high=1.0
        ),
        context_chunks=_int(
            section, "context_chunks", settings.rag_context_chunks, low=1, high=_MAX_CONTEXT_CHUNKS
        ),
        context_token_budget=_int(
            section,
            "context_token_budget",
            settings.rag_context_token_budget,
            low=1,
            high=_MAX_TOKEN_BUDGET,
        ),
        min_score_ratio=_float(
            section, "min_score_ratio", settings.rag_min_score_ratio, low=0.0, high=1.0
        ),
        query_history_turns=_int(
            section,
            "query_history_turns",
            settings.rag_query_history_turns,
            low=0,
            high=_MAX_HISTORY_TURNS,
        ),
    )


# 這一層認得的模式。**不是從 `app_settings` 的 Literal 推導**：那份型別是給環境變數用的，
# 而這裡的輸入是使用者寫得到的 KB config（2C 之後更是）。
#
# 四格而不是三格（2B-3）：沒有 `vector+rerank` 的話，`hybrid+rerank` 贏了也分不出是
# rerank 的功勞還是 hybrid 的——而 2B-2 的數據顯示 hybrid 目前是負貢獻，這個歸因問題
# 不是假想的。
_MODES = frozenset({"vector", "vector+rerank", "hybrid", "hybrid+rerank"})


def _mode(section: Mapping[str, Any], default: str) -> str:
    value = section.get("retrieval_mode", default)
    if isinstance(value, str) and value in _MODES:
        return value
    return _rejected("retrieval_mode", value, default)


def _int(section: Mapping[str, Any], key: str, default: int, *, low: int, high: int) -> int:
    value = section.get(key, default)
    # `bool` 是 `int` 的子類別，而 `{"top_k": true}` 的意思顯然不是 `top_k=1`。
    if isinstance(value, bool) or not isinstance(value, int):
        return _rejected(key, value, default)
    return max(low, min(high, value))


def _float(
    section: Mapping[str, Any], key: str, default: float, *, low: float, high: float
) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _rejected(key, value, default)
    return max(low, min(high, float(value)))


def _rejected[T](key: str, value: object, default: T) -> T:
    """壞值退回預設並留下線索。

    **不 raise**：這條路跑在使用者按下送出之後的背景生成裡，例外會讓整輪失敗，而
    使用者看到的是「一直出錯」而不是「我的設定填錯了」。記一筆 warning 才找得回來。
    """
    logger.warning("rag_param_rejected", param=key, value=repr(value))
    return default

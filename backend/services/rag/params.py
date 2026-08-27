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

**寫入時驗證、讀取時容忍。** 這裡是**讀取**那一半：壞值退回預設並記一筆，比讓那個
KB 從此問不了問題好——後者使用者看到的只是「一直出錯」。寫入端的驗證在
`services/knowledge/kb_config.py`（2B-5），**兩端共用該處的同一份參數宣告**：上下限
各寫一份的話，兩邊各自都會綠，而症狀是「後台填得進去的值，實際跑起來被夾成別的」。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from services.knowledge.kb_config import MAX_TOP_K, SECTIONS, read_param, section_of

logger = get_logger(__name__)

__all__ = ["MAX_TOP_K", "RagParams", "resolve_rag_params"]

# KB config 裡放檢索參數的區塊名。與 1B 的切塊參數（`config.chunk`）同一個慣例——
# 兩套命名的話，2C 的設定畫面要為每個功能各寫一次讀寫邏輯。
_SECTION = "retrieval"


@dataclass(frozen=True, slots=True)
class RagParams:
    """一次檢索要用的全部參數。**上層只認得這個型別**，不知道值從哪一層來。

    欄位名與 `SECTIONS["retrieval"]` 的鍵**逐字相同**，且由
    `test_kb_config_write.py::TestBoundsAreShared` 釘住：不同的話，寫入端會接受一個
    讀取端根本不看的鍵——它通過驗證、存進 DB、在設定畫面上顯示，然後完全不生效。
    """

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
    section = section_of(kb_config, _SECTION)
    specs = SECTIONS[_SECTION]
    values: dict[str, Any] = {
        key: read_param(specs, key, section, settings, on_rejected=_rejected) for key in specs
    }
    return RagParams(**values)


def _rejected(key: str, value: object) -> None:
    """壞值退回預設時留下線索。

    **不 raise**：這條路跑在使用者按下送出之後的背景生成裡，例外會讓整輪失敗，而
    使用者看到的是「一直出錯」而不是「我的設定填錯了」。記一筆 warning 才找得回來。
    """
    logger.warning("rag_param_rejected", param=key, value=repr(value))

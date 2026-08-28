"""reindex 的判定與狀態機（06 §2.2 的四步，2B-6）。

**與 `reindex.py` 分開的理由**：這裡的每一個判斷都會被三個地方各問一次——API 的
`needs_reindex`、worker 的每一批、清理器的保留窗。混在 service 裡的話，那三處只能
各自重寫一次條件，而那正是 2B-5 用 `kb_config.SECTIONS` 收掉的那一類漂移。

這一層**不碰 DB、不碰 KB model**：收數字、回數字。因此四步流程的每一個判斷都測得
到，而不必先造出一整個知識庫。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "REINDEX_ACTIVE_STATUSES",
    "STATUS_COMPLETED",
    "STATUS_EMBEDDING",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_RECHUNKING",
    "ReindexPlan",
    "ReindexProgress",
    "needs_reindex",
    "next_status",
    "plan_reindex",
    "ready_to_switch",
]

STATUS_PENDING = "pending"
STATUS_RECHUNKING = "rechunking"
STATUS_EMBEDDING = "embedding"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# 「同一個 KB 不得有兩個進行中的 job」那條 partial unique 的條件
# （`KbReindexJob.Meta.constraints`）。**兩邊必須是同一份**：少列一個狀態，卡在
# 那個狀態的 job 就擋不住第二次觸發，而兩個 job 會各自往同一批 chunk 寫不同版本的
# 向量，然後互相把對方切掉。
REINDEX_ACTIVE_STATUSES = frozenset({STATUS_PENDING, STATUS_RECHUNKING, STATUS_EMBEDDING})

_TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED})


@dataclass(frozen=True)
class ReindexPlan:
    """這一次要做哪幾件事。開跑時算一次，之後整個 job 都照它走。"""

    target_model: str
    target_embedding_version: int
    target_knowledge_version: int
    rechunk: bool


@dataclass(frozen=True)
class ReindexProgress:
    """完成度——第 3 步的原子切換就是靠它判定。

    分母是「這個 KB 現行的 active chunk 數」，分子是「其中已有**目標版本**向量的
    數量」。分母若寫成「有向量的 chunk 數」，它恆等於分子，於是永遠 100%——而那個
    100% 會讓 KB 在只算完一半時就切過去。
    """

    total_chunks: int
    embedded_chunks: int

    def __post_init__(self) -> None:
        if self.embedded_chunks > self.total_chunks:
            # 分子大於分母代表數錯了（最可能是把兩個版本的向量一起數進來）。
            # 靜靜地當成 100% 的話，切換會發生在只算完一部分的時候。
            raise ValueError(
                f"已完成數 {self.embedded_chunks} 大於總數 {self.total_chunks}——分子分母算錯了"
            )

    @property
    def ratio(self) -> float:
        # 空 KB（或文件都還沒 ready）要能走完，否則那個 job 永遠不會結束。
        if self.total_chunks == 0:
            return 1.0
        return self.embedded_chunks / self.total_chunks

    @property
    def is_complete(self) -> bool:
        return self.embedded_chunks >= self.total_chunks


def needs_reindex(*, knowledge_version: int, indexed_knowledge_version: int) -> bool:
    """既有的 chunk 是不是用**現在這組**切塊參數切出來的（2B-5 的 `knowledge_version`）。

    比「一不一樣」而不是「大不大」：把 chunk 參數改回上一組同樣會遞增
    `knowledge_version`（那是遞增計數器不是內容 hash），寫成 ``>`` 的話這種情況會
    被判成不需要重建，而既有 chunk 仍然是用中間那組參數切的。
    """
    return knowledge_version != indexed_knowledge_version


def plan_reindex(
    *,
    current_model: str,
    current_embedding_version: int,
    knowledge_version: int,
    indexed_knowledge_version: int,
    target_model: str | None,
    rechunk: bool | None = None,
) -> ReindexPlan:
    """決定目標與範圍。

    ``target_model`` 省略 = 沿用現行模型（切塊參數改完之後按「重建」的情況）。
    ``rechunk`` 省略 = 由 `needs_reindex` 判定；顯式傳入是給兩種情況用的：chunker
    本身改版（不會動 `knowledge_version`，只能手動要求重切），以及純換模型時明確
    表示不要順帶重切。
    """
    model = current_model if target_model is None else target_model.strip()
    if not model:
        # 空字串會照樣寫進 `UNIQUE(chunk, model, embedding_version)`（1C 的教訓）：
        # 它不報錯，只讓檢索永遠對不上，而症狀出現在幾十分鐘後的第 3 步。
        raise ValueError("target model 不得為空字串")

    should_rechunk = (
        needs_reindex(
            knowledge_version=knowledge_version,
            indexed_knowledge_version=indexed_knowledge_version,
        )
        if rechunk is None
        else rechunk
    )
    if should_rechunk and model != current_model:
        # **重切 ＋ 換模型不能是同一個 job**，而且這不是保守，是因為它沒有便宜的做法：
        # 重切產生的新 chunk 由正常的 ETL 路徑用 KB 現行模型算一次向量，換模型就得
        # 再算第二次（每個 chunk 兩次真的 API 呼叫）；要避開就得在重切**之前**先把
        # KB 的模型切過去，而那會讓還沒輪到重切的文件在整段期間查不到——一次影響
        # 整個知識庫，而不是逐份文件的那幾分鐘。
        # 分兩次跑沒有這個問題：先重切（沿用現行模型），完成後再發一次換模型的重建。
        raise ValueError("重切與換 embedding 模型請分兩次執行：先重建切塊，完成後再換模型")
    return ReindexPlan(
        target_model=model,
        # **重嵌入要遞增，重切不要**（2026-08-28 實作時的定案，見 13 §4 的 2B-6 紀錄）。
        #
        # 遞增的用途只有一個：讓新舊兩版向量並存，好讓既有 chunk 在重算期間**繼續
        # 服務檢索**（06 §2.2 第 1 步）。沿用現行版本號的話，新向量會與舊的撞鍵，
        # 而 upsert 之下那是就地覆蓋——並存當場失效，也沒有東西可以回退。
        #
        # 重切沒有這個需求，而且遞增會讓它付兩次錢：re-ingest 產生的是**全新的
        # chunk 列**（舊的當場標 superseded、退出檢索，1B-6 起就是如此），沒有任何
        # 東西需要並存；而那些新 chunk 會由正常的 ETL→embedding 路徑算一次向量，
        # 用的是 KB 的**現行**版本號。這時若目標是 current+1，reindex 會為同一批
        # chunk 再算一次——每個 chunk 兩次真的 API 呼叫，而結果一模一樣。
        target_embedding_version=(
            current_embedding_version if should_rechunk else current_embedding_version + 1
        ),
        # 不重切就不動它：重建完成時 `indexed_knowledge_version` 會被設成這個值，
        # 而沒有重切過的 chunk 不該被宣稱是新版設定切出來的。
        target_knowledge_version=knowledge_version if should_rechunk else indexed_knowledge_version,
        rechunk=should_rechunk,
    )


def next_status(status: str, *, rechunk: bool) -> str:
    """狀態機的下一站。

    重切的 job 一定先走 ``rechunking``，而 ``rechunking`` 的下一站是 ``embedding``
    **不是** ``completed``——重切完的新 chunk 一個向量都沒有，而舊 chunk 已經標成
    superseded 退出檢索。在那裡收工等於把知識庫清空。
    """
    if status in _TERMINAL_STATUSES:
        return status
    if status == STATUS_PENDING:
        return STATUS_RECHUNKING if rechunk else STATUS_EMBEDDING
    if status == STATUS_RECHUNKING:
        return STATUS_EMBEDDING
    return STATUS_COMPLETED


def ready_to_switch(*, status: str, progress: ReindexProgress) -> bool:
    """可以做第 3 步了嗎——**整個工作包唯一不可逆的一步**。

    必須同時是「已經在算向量」與「算完了」。少了前者的話，一個還在重切階段的 job
    會因為「目前存在的 chunk 剛好都有向量」而滿足 100%——而那時大部分文件根本還沒
    被重新切過。
    """
    return status == STATUS_EMBEDDING and progress.is_complete

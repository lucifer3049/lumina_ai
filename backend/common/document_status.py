"""文件狀態機的唯一定義（08 §2；2026-08-30 深度審查的收斂）。

**在此之前，合法狀態集合散落在六個以上的位置各自手寫**（ingestion、embedding、
documents、rescue、reindex、model default），前端還照抄一份。散落的代價不是重複，
是**漂移沒有症狀**：新增或改名一個狀態時漏掉哪一份，哪一份的判斷就對著一個不存在
的世界運作——rescue 掃不到、embedding 的防呆放行、re-ingest 的擋線失守，而這些全都
不會報錯。收斂之後，每一份「哪些狀態算 X」的判斷都指回這裡的**具名集合**，集合的
定義與它存在的理由寫在同一個地方。

放 `common/` 而不是 model：`services/` 禁止 import `apps.*.models`（鐵則），而狀態
集合正是 service 層天天要用的東西；`common/` 不 import 任何其他層，誰都到得了。

成功路徑（順序即進度）::

    uploaded → parsing → cleaned → chunked → embedding → ready

``failed`` 是另一種結局，不是一格進度；任何處理中狀態都可能落入。前端的對應表在
`frontend/src/utils/documentStatus.ts`（openapi 對 status 是裸字串，前端必然自帶
一份）——`tests/unit/test_document_status.py` 的對帳測試釘住兩邊不漂移。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "EMBEDDABLE_STATUSES",
    "IN_PROGRESS_STATUSES",
    "RESCUABLE_STATUSES",
    "STILL_PROCESSING_STATUSES",
    "TERMINAL_STATUSES",
    "DocumentStatus",
]


class DocumentStatus(StrEnum):
    """`documents.status` 的合法值。StrEnum：與 DB 裡的字串直接互換、可直接落地。"""

    UPLOADED = "uploaded"
    PARSING = "parsing"
    CLEANED = "cleaned"
    CHUNKED = "chunked"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"


# 終點：只有這兩個。輪詢停不停、rescue 掃不掃、通知發不發，都以它為準。
TERMINAL_STATUSES = frozenset({DocumentStatus.READY, DocumentStatus.FAILED})

# 「有一個 job 正在寫這份文件」的狀態——re-ingest 在這幾格要拒絕（409），否則兩個
# job 同時寫同一份文件的 chunk，先寫完的會被另一個的「清同版殘留」刪掉，隨機少一半
# 內容且兩邊都不報錯。``uploaded``/``chunked`` 不在其中：那兩格是「該有一則訊息，
# 而它還沒被處理」，重跑安全，同時是佇列訊息遺失時唯一的恢復入口。
IN_PROGRESS_STATUSES = frozenset(
    {DocumentStatus.PARSING, DocumentStatus.CLEANED, DocumentStatus.EMBEDDING}
)

# 停滯救援（rescue）只補送這兩格——它們的共通點是「該有一則訊息，而它不在」。
# 與 `IN_PROGRESS_STATUSES` 恰為處理中狀態的互補：做到一半的不能補送（會出現兩個
# writer），要嘛它還活著、要嘛等它逾時走 failed。
RESCUABLE_STATUSES = frozenset({DocumentStatus.UPLOADED, DocumentStatus.CHUNKED})

# embedding 允許進場的狀態（``chunked`` 正常、``embedding``/``ready`` 是冪等重跑）。
# **這是防呆不是樂觀鎖**——訊息可能比世界舊；真正的競態守門在寫入端
# （`DocumentRepository.set_status` 的 expected_* 條件），這裡擋掉的是明顯走錯門的。
EMBEDDABLE_STATUSES = frozenset(
    {DocumentStatus.CHUNKED, DocumentStatus.EMBEDDING, DocumentStatus.READY}
)

# 「還在跑」＝所有非終局。KB 重建的重切階段要等文件全部離開這一組才進 embedding；
# 用補集定義而不是逐一列舉，新增中間狀態時它自動正確。
STILL_PROCESSING_STATUSES = frozenset(DocumentStatus) - TERMINAL_STATUSES

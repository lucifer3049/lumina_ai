"""驗收：文件狀態機的唯一定義（`common.document_status`，2026-08-30 收斂）。

在此之前，合法狀態集合散落在六個以上的位置各自手寫，前端還照抄一份。散落的代價是
**漂移沒有症狀**：新增或改名狀態時漏掉哪一份，哪一份的判斷就對著不存在的世界運作
——rescue 掃不到、embedding 防呆放行、re-ingest 擋線失守，全都不報錯。

這一檔釘兩件事：集合之間的關係（定義自洽），以及**前端那份照抄的清單沒有漂**
（openapi 對 status 是裸字串，前端必然自帶一份；跨語言只能用對帳測試釘，形式同
test_ci_pipeline 讀 Makefile）。
"""

from __future__ import annotations

from pathlib import Path

from common.document_status import (
    EMBEDDABLE_STATUSES,
    IN_PROGRESS_STATUSES,
    RESCUABLE_STATUSES,
    STILL_PROCESSING_STATUSES,
    TERMINAL_STATUSES,
    DocumentStatus,
)

_FRONTEND_COPY = (
    Path(__file__).resolve().parents[3] / "frontend" / "src" / "utils" / "documentStatus.ts"
)


class TestTheSetsAreSelfConsistent:
    def test_the_wire_values_are_the_documented_seven(self) -> None:
        """08 §2 的狀態機。改這裡＝改 API 上的字串＝前端與既有資料都要跟著動——
        這條測試把「那是一個要人類簽核的決定」變成紅燈。"""
        assert {status.value for status in DocumentStatus} == {
            "uploaded",
            "parsing",
            "cleaned",
            "chunked",
            "embedding",
            "ready",
            "failed",
        }

    def test_terminal_and_still_processing_partition_the_machine(self) -> None:
        """「還在跑」＝所有非終局：用補集定義，新增中間狀態時自動正確。"""
        assert frozenset(DocumentStatus) == TERMINAL_STATUSES | STILL_PROCESSING_STATUSES
        assert not TERMINAL_STATUSES & STILL_PROCESSING_STATUSES

    def test_rescuable_and_in_progress_are_disjoint(self) -> None:
        """可補送（訊息不在飛）與進行中（有 writer）互斥——重疊的那個狀態會被
        rescue 補送出第二個 writer，兩個 job 互刪對方的 chunk。"""
        assert not RESCUABLE_STATUSES & IN_PROGRESS_STATUSES
        assert RESCUABLE_STATUSES | IN_PROGRESS_STATUSES == STILL_PROCESSING_STATUSES

    def test_terminal_states_are_not_embeddable_entry_points_except_ready(self) -> None:
        """``failed`` 不可進 embedding（重跑入口是 re-ingest）；``ready`` 可以
        （at-least-once 的重送要無害通過）。"""
        assert DocumentStatus.FAILED not in EMBEDDABLE_STATUSES
        assert DocumentStatus.READY in EMBEDDABLE_STATUSES

    def test_members_interoperate_with_raw_db_strings(self) -> None:
        """StrEnum 的存在理由：DB 讀出來的裸字串要能直接進集合判斷，不需要到處轉型。"""
        assert "chunked" in EMBEDDABLE_STATUSES
        assert "parsing" in IN_PROGRESS_STATUSES
        assert str(DocumentStatus.READY) == "ready"


class TestTheFrontendCopyHasNotDrifted:
    """`frontend/src/utils/documentStatus.ts` 是刻意的照抄（該檔 docstring 有記）。
    照抄會漂，而漂移的症狀在前端：輪詢停在新狀態、按鈕在錯的狀態亮起。"""

    def test_every_status_appears_in_the_frontend_module(self) -> None:
        source = _FRONTEND_COPY.read_text(encoding="utf-8")

        for status in DocumentStatus:
            assert f"'{status.value}'" in source, (
                f"前端的 documentStatus.ts 缺 {status.value}——兩份清單漂了"
            )

    def test_the_frontend_success_path_order_matches(self) -> None:
        """`DOCUMENT_STAGES` 是進度條的格子，順序即進度——順序錯了進度條會倒退。"""
        normalized = " ".join(_FRONTEND_COPY.read_text(encoding="utf-8").split())
        expected = "'uploaded', 'parsing', 'cleaned', 'chunked', 'embedding', 'ready'"

        assert expected in normalized, "前端 DOCUMENT_STAGES 的內容或順序與後端狀態機不一致"

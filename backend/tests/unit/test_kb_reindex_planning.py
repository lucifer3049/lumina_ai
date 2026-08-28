"""驗收：reindex 的**判定與狀態機**（06 §2.2 的四步，工作包 2B-6）。

這一檔只驗不碰 DB 的那一半：**要不要重建、下一步是什麼、可以切換了嗎**。
放 unit 層的理由是這些判斷會被三個地方各問一次（API 的 `needs_reindex`、worker 的
每一批、清理器的保留窗），而「各自算一次」正是 2B-5 剛用 `kb_config.SECTIONS` 修掉
的那一類漂移。

06 §2.2 的四步：

    1. KB 設定新 model/version（**舊版持續服務查詢**）
    2. 背景批次：對全部 active chunks 產生新版 embeddings（限速）
    3. 完成度 100% → KB.embedding_version 原子切換 → 查詢改用新版
    4. 觀察期（可回退）→ 清理 Job 刪舊版 embeddings

四個錯了都不會有例外的地方：

1. **第 1 步就把 KB 切過去**。新向量還沒算完，而檢索已經照新的 `(model, version)`
   去查——那個組合一列都對不上（`UNIQUE(chunk, model, embedding_version)`），
   於是整個知識庫在重建的那幾十分鐘裡回零筆，而 API 全部 200。
2. **完成度算錯分母**。用「有向量的 chunk 數」當分母的話它恆等於分子，永遠 100%；
   要比的是「這個 KB 現行的 active chunk 數」與「其中已有新版向量的數量」。
3. **重切完成就當作重建完成**。切塊變更的完整代價是「重切 + 重算」，只做前半的話
   新 chunk 一個向量都沒有，而 `superseded` 的舊 chunk 已經退出檢索——知識庫變空。
4. **保留窗從 job 建立時間起算**。可回退的窗口是從**切換**那一刻開始的，用建立時間
   算等於「重建跑得愈久、可回退的時間愈短」，而跑得久的正是最該留退路的那些。
"""

from __future__ import annotations

import pytest

from services.knowledge.reindex_plan import (
    REINDEX_ACTIVE_STATUSES,
    ReindexPlan,
    ReindexProgress,
    needs_reindex,
    next_status,
    plan_reindex,
    ready_to_switch,
)


class TestNeedsReindex:
    """「這個 KB 需要重建嗎」——2B-5 的 `knowledge_version` 到這裡才有消費者。"""

    def test_a_fresh_kb_does_not(self) -> None:
        assert needs_reindex(knowledge_version=1, indexed_knowledge_version=1) is False

    def test_a_chunking_change_does(self) -> None:
        """`knowledge_version` 只在切塊區的值變動時遞增（2B-5），所以它一動就是真的。"""
        assert needs_reindex(knowledge_version=2, indexed_knowledge_version=1) is True

    def test_it_compares_versions_not_ordering(self) -> None:
        """比的是「一不一樣」而不是「大不大」。

        回退設定（把 chunk 參數改回上一組）同樣會遞增 `knowledge_version`——那是
        遞增計數器不是內容 hash。寫成 `>` 的話這種情況判成「不需要重建」，而既有
        chunk 仍然是用中間那組參數切出來的。
        """
        assert needs_reindex(knowledge_version=2, indexed_knowledge_version=3) is True


class TestPlan:
    """`plan_reindex` 決定這一次要做哪幾件事（重切？重嵌入？目標是什麼）。"""

    def test_model_upgrade_targets_the_next_embedding_version(self) -> None:
        """並存是靠 `(chunk, model, embedding_version)` 這個唯一鍵（05 §3.2）。

        目標版本不遞增的話，新向量會與舊的**撞鍵**——upsert 之下那是就地覆蓋，
        第 1 步的「舊版持續服務」當場失效，而且沒有任何東西可以回退。
        """
        plan = plan_reindex(
            current_model="text-embedding-3-small",
            current_embedding_version=1,
            knowledge_version=1,
            indexed_knowledge_version=1,
            target_model="gemini-embedding-2",
        )

        assert plan == ReindexPlan(
            target_model="gemini-embedding-2",
            target_embedding_version=2,
            target_knowledge_version=1,
            rechunk=False,
        )

    def test_a_rechunk_keeps_the_current_embedding_version(self) -> None:
        """**重切不遞增版本號**（2026-08-28 實作時推翻本檔原本的寫法）。

        遞增的用途只有「讓既有 chunk 在重算期間繼續服務檢索」。重切沒有這個需求
        ——re-ingest 產生的是全新的 chunk 列，舊的當場標 superseded 退出檢索（1B-6）
        ——而遞增會讓它付兩次錢：新 chunk 由正常的 ETL→embedding 路徑用 KB **現行**
        版本號算過一次，reindex 若以 current+1 為目標就得為同一批再算一次，兩次的
        結果一模一樣。
        """
        plan = plan_reindex(
            current_model="text-embedding-3-small",
            current_embedding_version=3,
            knowledge_version=2,
            indexed_knowledge_version=1,
            target_model=None,
        )

        assert plan.rechunk is True
        assert plan.target_model == "text-embedding-3-small"
        assert plan.target_embedding_version == 3

    def test_rechunking_and_changing_model_at_once_is_rejected(self) -> None:
        """兩件事同一個 job 沒有便宜的做法（見 `plan_reindex` 的註解）：不是重算兩次，
        就是讓整個知識庫在重切期間查不到。分兩次跑兩者都沒有。"""
        with pytest.raises(ValueError, match="分兩次"):
            plan_reindex(
                current_model="a",
                current_embedding_version=1,
                knowledge_version=2,
                indexed_knowledge_version=1,
                target_model="b",
            )

    def test_a_chunking_change_makes_it_a_rechunk(self) -> None:
        plan = plan_reindex(
            current_model="m",
            current_embedding_version=1,
            knowledge_version=5,
            indexed_knowledge_version=4,
            target_model=None,
        )

        assert plan.rechunk is True
        assert plan.target_knowledge_version == 5, "重切的目標是**開跑當下**的版本"

    def test_a_model_upgrade_alone_does_not_rechunk(self) -> None:
        """換 embedding 模型不需要重切：chunk 是文字，與用哪個模型算向量無關。

        順手重切的話，一次換模型會連帶產生整庫的新 chunk 與新 doc_version，
        而使用者要的只是換模型。
        """
        plan = plan_reindex(
            current_model="a",
            current_embedding_version=1,
            knowledge_version=7,
            indexed_knowledge_version=7,
            target_model="b",
            rechunk=False,
        )

        assert plan.rechunk is False

    def test_rechunk_can_be_forced_even_without_a_config_change(self) -> None:
        """chunker 本身改版（1B-5 之後的每一次）不會動 `knowledge_version`。

        沒有這個出口的話，唯一的重切方式是「隨便改一個 chunk 參數再改回來」——
        那會在稽核上留下兩筆假的設定變更。
        """
        plan = plan_reindex(
            current_model="a",
            current_embedding_version=1,
            knowledge_version=1,
            indexed_knowledge_version=1,
            target_model=None,
            rechunk=True,
        )

        assert plan.rechunk is True

    def test_an_empty_target_model_is_rejected(self) -> None:
        """空字串會照樣寫進 `UNIQUE(chunk, model, embedding_version)`（1C 的教訓）。

        它不會報錯，只會讓檢索永遠對不上——而錯誤發生在幾十分鐘後的第 3 步。
        """
        with pytest.raises(ValueError, match="model"):
            plan_reindex(
                current_model="a",
                current_embedding_version=1,
                knowledge_version=1,
                indexed_knowledge_version=1,
                target_model="   ",
            )


class TestProgress:
    """完成度——第 3 步的原子切換就是靠它判定。"""

    def test_the_denominator_is_the_active_chunk_count(self) -> None:
        progress = ReindexProgress(total_chunks=100, embedded_chunks=25)

        assert progress.ratio == 0.25
        assert progress.is_complete is False

    def test_complete_means_every_active_chunk_has_a_new_vector(self) -> None:
        assert ReindexProgress(total_chunks=100, embedded_chunks=100).is_complete is True

    def test_an_empty_kb_is_complete_not_a_division_by_zero(self) -> None:
        """空 KB（或全部文件都還沒 ready）要能走完，否則那個 job 永遠不會結束。"""
        progress = ReindexProgress(total_chunks=0, embedded_chunks=0)

        assert progress.ratio == 1.0
        assert progress.is_complete is True

    def test_more_vectors_than_chunks_is_not_complete_by_accident(self) -> None:
        """分子大於分母代表數錯了（例如把兩個版本的向量一起數進來）。

        當成 100% 的話，第 3 步會在只算完一半時切換過去。
        """
        with pytest.raises(ValueError):
            ReindexProgress(total_chunks=10, embedded_chunks=11)


class TestReadyToSwitch:
    """第 3 步的閘門。**這是整個工作包唯一不可逆的一步**。"""

    def test_embedding_complete_is_required(self) -> None:
        assert ready_to_switch(status="embedding", progress=ReindexProgress(10, 10)) is True
        assert ready_to_switch(status="embedding", progress=ReindexProgress(10, 9)) is False

    def test_a_job_still_rechunking_never_switches(self) -> None:
        """重切階段的「完成度」算的是文件，不是 chunk——這時的 chunk 數還在變。

        少了這一條，一份剛好已經重切完的文件會讓 10/10 成立，而其餘幾百份還沒動。
        """
        assert ready_to_switch(status="rechunking", progress=ReindexProgress(10, 10)) is False

    @pytest.mark.parametrize("status", ["pending", "completed", "failed"])
    def test_terminal_and_unstarted_jobs_never_switch(self, status: str) -> None:
        assert ready_to_switch(status=status, progress=ReindexProgress(10, 10)) is False


class TestStatusMachine:
    def test_a_rechunking_job_goes_through_rechunking_first(self) -> None:
        assert next_status("pending", rechunk=True) == "rechunking"

    def test_a_model_only_job_skips_straight_to_embedding(self) -> None:
        assert next_status("pending", rechunk=False) == "embedding"

    def test_rechunking_hands_over_to_embedding(self) -> None:
        """重切完不等於重建完——新 chunk 這時一個向量都沒有（本檔第 3 個陷阱）。"""
        assert next_status("rechunking", rechunk=True) == "embedding"

    def test_embedding_hands_over_to_completed(self) -> None:
        assert next_status("embedding", rechunk=True) == "completed"

    @pytest.mark.parametrize("status", ["completed", "failed"])
    def test_terminal_statuses_stay_put(self, status: str) -> None:
        assert next_status(status, rechunk=True) == status

    def test_active_statuses_are_exactly_the_non_terminal_ones(self) -> None:
        """這組常數是「同一個 KB 不得有兩個進行中的 job」那條 DB 約束的條件。

        少列一個狀態，卡在那個狀態的 job 就擋不住第二次觸發——兩個 job 會各自
        往同一批 chunk 寫不同版本的向量，然後互相把對方切掉。
        """
        assert sorted(REINDEX_ACTIVE_STATUSES) == ["embedding", "pending", "rechunking"]

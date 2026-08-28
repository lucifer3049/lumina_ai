"""驗收：檢索參數的單一來源與覆寫順序（06 §3.1「KB 可覆寫」，13 §3 工作包 1D-5）。

**參數不准寫死在邏輯裡**（2026-08-17 產品決定）：凡是「要由使用者決定」的數字，
落點只有一個具名的地方，覆寫順序固定：

    系統預設（app_settings，環境變數可蓋）
      → 租戶設定（09 §2.6 的 `/settings`，屬 2C——現在還沒有這一層）
        → KB 覆寫（05 §3.2 的 `knowledge_base.config`）

後台那個統一設定畫面屬 2C。**這一層現在就要存在**，理由不是為了現在能調——是為了
2C 只需要接線而不必回頭重構：數字一旦散進 `RetrievalService` 與 `ChatService`，
之後要蒐齊它們得逐檔翻，而漏掉一個的症狀是「後台改了沒有反應」。

三件事錯了都不會有例外：

1. **KB 只覆寫一個鍵，其餘卻跟著回到預設**。使用者只調了 top_k，結果 context 上限
   也被重設——而他改的那一項確實生效了，所以他不會懷疑這裡。
2. **壞值讓查詢整個爆掉**。KB config 是使用者寫得到的 JSON（2C 之後更是），一個
   `"top_k": "很多"` 不該讓那個 KB 從此問不了問題。
3. **上限沒有夾住**。`top_k` 直接進 SQL 的 LIMIT，一個極大值不會失敗，只會讓那幾秒
   對**所有租戶**都很慢。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from config.settings.app_settings import AppSettings, get_app_settings
from services.rag.params import MAX_TOP_K, resolve_rag_params


class TestSystemDefaults:
    def test_they_follow_the_design_document(self) -> None:
        """預設值對映 06 §3.1（vector top_k=40、rerank top_n 6~8）與 §3.2
        （RAG context ~4,500 token）。

        **釘住它們是因為這些數字是憑證據調的**（13 §開發流程：文件值是起始點，調整需
        在 PR 說明引用評測／壓測數據）。改動時這條會紅，而那正是要求說明理由的地方。
        """
        settings = get_app_settings()

        assert settings.rag_top_k == 40
        # 2B-2 的四個新旋鈕（06 §3.1 的 FTS top_k=40、Hybrid RRF k=60 → 24）。
        assert settings.rag_fts_top_k == 40
        assert settings.rag_rrf_k == 60
        assert settings.rag_hybrid_candidates == 24
        # **預設 `vector+rerank`**（2026-08-27 使用者裁決，2B-5）：兩份題組上贏的是
        # rerank 而不是 hybrid——手寫題 recall@1 0.4375 → 0.7917，而 hybrid 那一路的
        # 邊際貢獻逐題為零。數據與理由見 `app_settings.rag_retrieval_mode` 的註解。
        # **hybrid 不進預設但程式全留**：識別符密集的語料仍是 FTS 該贏的地方，走 KB
        # 層的 `retrieval_mode` 覆寫。
        assert settings.rag_retrieval_mode == "vector+rerank"
        # 06 §3.1 的絕對門檻 0.3——**預設關閉（0），且 0.3 這個值已被資料推翻**
        # （2B-5 第四次評測：手寫題上正解與非正解的分數重疊，0.3 會砍掉 56% 的正解、
        # 14/24 題連一段正解都不剩）。要擋「無相關內容」只能走相對門檻。
        assert settings.rag_rerank_threshold == 0.0
        assert settings.rag_context_chunks == 8
        assert settings.rag_context_token_budget == 4500

    def test_the_relative_score_gate_is_off_by_default(self) -> None:
        """相對門檻預設關閉（1D-5 決定）。

        06 §3.1 的絕對門檻 0.3 是 **rerank（cross-encoder）分數**，而 Phase 1 只有
        餘弦相似度——兩者是不同尺度的數字，把 0.3 套上去等於拿 100 分制的及格線去
        砍 10 分制的考卷，結果是每次都回「知識庫中找不到相關內容」。

        相對門檻（只留下分數接近第一名的那幾張）不吃尺度，所以它是 Phase 1 唯一
        安全的形式；但它仍會砍掉東西，因此**預設是關的**，開不開由使用者用資料決定。
        絕對門檻等 2B 接上開源 rerank（`bge-reranker-v2-m3`，MIT）之後才有意義。
        """
        assert get_app_settings().rag_min_score_ratio == 0.0

    def test_resolving_without_a_knowledge_base_gives_the_system_defaults(self) -> None:
        settings = get_app_settings()

        params = resolve_rag_params(None)

        assert params.top_k == settings.rag_top_k
        assert params.context_chunks == settings.rag_context_chunks
        assert params.context_token_budget == settings.rag_context_token_budget
        assert params.min_score_ratio == settings.rag_min_score_ratio
        assert params.query_history_turns == settings.rag_query_history_turns


class TestKnowledgeBaseOverride:
    def test_one_key_overrides_only_itself(self) -> None:
        """**只蓋有給的那一個。** 整包取代的話，使用者調了 top_k 就會把 context 上限
        一起重設——而他改的那一項確實生效了，所以他不會懷疑到這裡。"""
        params = resolve_rag_params({"retrieval": {"context_chunks": 3}})

        assert params.context_chunks == 3
        assert params.top_k == get_app_settings().rag_top_k

    def test_the_section_name_matches_the_chunk_settings(self) -> None:
        """KB config 的形狀是 `{"chunk": {...}, "retrieval": {...}}`——與 1B 的
        `_chunk_config_from` 同一個慣例。兩套命名的話，後台設定畫面要為每個功能各寫
        一次讀寫邏輯，而那正是「統一管理」要避免的事。"""
        assert resolve_rag_params({"chunk": {"target_tokens": 1}}).top_k == 40

    def test_unknown_keys_are_ignored(self) -> None:
        """KB config 是使用者寫得到的 JSON。未知的鍵直接展開成建構參數的話，一個
        打錯的欄位會變成 `TypeError`——而它發生在使用者按下送出之後的背景生成裡。"""
        params = resolve_rag_params({"retrieval": {"top_kk": 5, "亂寫": True}})

        assert params.top_k == 40

    def test_a_value_of_the_wrong_type_falls_back(self) -> None:
        """`{"top_k": "很多"}` 不該讓這個 KB 從此問不了問題。

        寫入時擋下來才是對的（2C 的設定畫面），但**讀取端在熱路徑上，要能容忍**：
        壞值退回預設並記一筆，比整輪失敗好——使用者看到的會是「一直出錯」。
        """
        params = resolve_rag_params({"retrieval": {"top_k": "很多", "min_score_ratio": None}})

        assert params.top_k == 40
        assert params.min_score_ratio == 0.0

    def test_out_of_range_values_are_clamped(self) -> None:
        """`top_k` 直接進 SQL 的 LIMIT。沒有上限的話，一個 `top_k=1000000` 不會失敗，
        只會讓 pgvector 把整個 KB 掃出來排序——那幾秒對**所有租戶**都很慢。"""
        params = resolve_rag_params({"retrieval": {"top_k": 1_000_000, "context_chunks": 0}})

        assert params.top_k == MAX_TOP_K
        assert params.context_chunks == 1

    def test_the_relative_gate_stays_within_zero_and_one(self) -> None:
        """比例大於 1 的意思是「只留下比第一名還高分的」——那永遠是空的，而症狀是
        這個 KB 突然什麼都答不出來。"""
        assert resolve_rag_params({"retrieval": {"min_score_ratio": 5}}).min_score_ratio == 1.0
        assert resolve_rag_params({"retrieval": {"min_score_ratio": -1}}).min_score_ratio == 0.0


class TestHybridParams:
    """2B-2 的四個旋鈕，全部走同一條解析（15 §4.1）。"""

    def test_kb_can_override_each_of_them(self) -> None:
        params = resolve_rag_params(
            {
                "retrieval": {
                    "fts_top_k": 10,
                    "rrf_k": 20,
                    "hybrid_candidates": 5,
                    "retrieval_mode": "vector",
                }
            }
        )

        assert (params.fts_top_k, params.rrf_k, params.hybrid_candidates) == (10, 20, 5)
        assert params.retrieval_mode == "vector"

    def test_fts_top_k_is_capped_like_the_vector_one(self) -> None:
        """它同樣直接進 SQL 的 LIMIT（`ChunkRepository.search_fts`）——沒有上限的話，
        一個極大值不會失敗，只會讓那幾秒對**所有租戶**都很慢。"""
        params = resolve_rag_params({"retrieval": {"fts_top_k": 10_000}})

        assert params.fts_top_k == MAX_TOP_K

    def test_an_unknown_mode_falls_back_to_the_default(self) -> None:
        """**壞值退回預設並記一筆**（讀取時容忍，同其他參數的理由）：KB config 是使用者
        寫得到的 JSON，一個 `"retrieval_mode": "hybird"` 不該讓那個 KB 從此問不了問題。
        """
        params = resolve_rag_params({"retrieval": {"retrieval_mode": "hybird"}})

        # 退回的是**系統預設**（`app_settings`），不是某個寫死的字串——2B-5 把預設從
        # `vector` 改成 `vector+rerank` 時，這條若比對字面值就會變成「壞值會讓那個 KB
        # 悄悄少掉 rerank」而測試照樣綠。
        assert params.retrieval_mode == get_app_settings().rag_retrieval_mode


class TestRerankParams:
    """2B-3 的旋鈕。"""

    def test_the_kb_can_set_an_absolute_threshold(self) -> None:
        assert resolve_rag_params({"retrieval": {"rerank_threshold": 0.3}}).rerank_threshold == 0.3

    def test_the_threshold_is_clamped_to_the_scale(self) -> None:
        """cross-encoder 的分數是 0~1；讓 KB 設 5 的話那個 KB 從此答不出任何問題。"""
        assert resolve_rag_params({"retrieval": {"rerank_threshold": 5}}).rerank_threshold == 1.0
        assert resolve_rag_params({"retrieval": {"rerank_threshold": -1}}).rerank_threshold == 0.0

    def test_the_rerank_modes_are_accepted_now(self) -> None:
        """2B-2 時它們被擋掉（設了也不會有事發生）；2B-3 起 rerank 真的存在。"""
        for mode in ("vector+rerank", "hybrid+rerank"):
            assert (
                resolve_rag_params({"retrieval": {"retrieval_mode": mode}}).retrieval_mode == mode
            )


class TestMockRerankIsRejectedInProduction:
    """`MockRerankProvider` 不准在正式環境當 cross-encoder 用（2B-5）。

    這條與 `rag_retrieval_mode` 的預設值是**同一個決定的兩半**：預設值把 rerank
    打開，這條把「打開了卻接著假的 reranker」擋掉。只留其中一半的話，正式環境會拿
    字元重疊比例排序，而 `rag_trace` 裡 `applied=True`、分數在 0~1、順序看起來合理
    ——沒有任何地方顯示它沒在工作。
    """

    def _settings(self, **overrides: Any) -> AppSettings:
        """不讀 `.env`：讀了的話這幾條會隨開發機的設定而綠或紅。"""
        base: dict[str, Any] = {
            "environment": "production",
            "rag_retrieval_mode": "vector+rerank",
            "ai_rerank_provider": "mock",
        }
        base.update(overrides)
        return AppSettings(_env_file=None, **base)  # type: ignore[call-arg]

    def test_production_with_mock_rerank_fails_fast(self) -> None:
        with pytest.raises(ValidationError, match="mock"):
            self._settings()

    @pytest.mark.parametrize("provider", ["tei", "jina"])
    def test_a_real_reranker_is_accepted(self, provider: str) -> None:
        assert self._settings(ai_rerank_provider=provider).ai_rerank_provider == provider

    def test_a_mode_without_rerank_is_accepted(self) -> None:
        """沒有要 rerank 的話，provider 是什麼都不重要——它根本不會被呼叫。"""
        assert self._settings(rag_retrieval_mode="vector").ai_rerank_provider == "mock"
        assert self._settings(rag_retrieval_mode="hybrid").ai_rerank_provider == "mock"

    @pytest.mark.parametrize("environment", ["development", "test"])
    def test_development_and_test_may_use_the_mock(self, environment: str) -> None:
        """開發與 CI 必須能在沒有 GPU 的機器上跑完整條 RAG 路徑。

        讓它們為此炸掉的話，唯一的出路是把模式改回 `vector`——而那會讓測試涵蓋的
        路徑與正式環境的不是同一條，這比 mock 本身危險。
        """
        assert self._settings(environment=environment).ai_rerank_provider == "mock"

    def test_hybrid_plus_rerank_is_covered_too(self) -> None:
        """守門看的是「模式含不含 rerank」，不是「等不等於 vector+rerank」。

        寫成等值比較的話，`hybrid+rerank` 會從這條檢查底下溜過去——而它是四個模式
        裡最容易被人手動設上去的那一個（06 §3.1 的設計就長那樣）。
        """
        with pytest.raises(ValidationError, match="mock"):
            self._settings(rag_retrieval_mode="hybrid+rerank")

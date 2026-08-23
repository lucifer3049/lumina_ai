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

from config.settings.app_settings import get_app_settings
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
        # **預設是 `vector` 而不是 06 §3.1 設計的 hybrid**（2026-08-23 使用者裁決）：
        # 三種 FTS 策略在兩份 golden set 上都沒讓 hybrid 勝出，而管線少了 rerank 這個
        # 裁判。數據與理由見 `app_settings.rag_retrieval_mode` 的註解；2B-4 接上
        # reranker 後用同一套評測再決定要不要翻回來。
        assert settings.rag_retrieval_mode == "vector"
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

        assert params.retrieval_mode == "vector"

    def test_rerank_mode_is_not_accepted_yet(self) -> None:
        """`hybrid+rerank` 要等 2B-3／2B-4。提前接受它的話，KB 設了之後**什麼都不會
        發生**——而使用者會以為 rerank 已經在跑了。"""
        params = resolve_rag_params({"retrieval": {"retrieval_mode": "hybrid+rerank"}})

        assert params.retrieval_mode == "vector"

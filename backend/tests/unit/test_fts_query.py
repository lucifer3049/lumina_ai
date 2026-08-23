"""驗收：全文檢索的查詢建構與索引宣告（13 §4 工作包 2B-1／2B-2b；06 §3.1 的 FTS 一路）。

**2B-2b 換掉了 2B-1 的查詢策略，理由是評測數據。** 2B-1 把整句問句送進 `&@*`
（similar search），2B-2 的評測顯示那樣比純向量更差（手寫題 recall@1 0.4375 → 0.3958、
DRCD 0.9417 → 0.9333）。追因的三個實測：

- `ef_search` 單獨查 → 正解在前三名；同一個詞放進
  `「向量索引的 ef_search 建議從多少開始調？」` → **0 筆**（中文 bigram 稀釋掉它）。
- 改成把整句切成 bigram 再 OR → 更糟：pgroonga 的分數沒有 IDF，比的是命中次數，
  長段落靠字數贏，正解掉出前五名。
- `fts_top_k` 從 40 降到 5 → 分數**一模一樣**：傷害來自前五名，不是候選數。

因此現在的規則是「**識別符才發言**」：問句裡有 `ef_search`、`ES256`、`pgBackRest`、
`PITR`、`第 14 條` 的 `14` 這種詞時才查，否則那一路棄權（回空字串，連 DB 都不打）。
沒有話講的時候閉嘴，比投一張模糊票好——後者會把向量找對的答案擠下去。

本檔驗兩件事：那條規則（純函式），以及索引宣告本身（讀 migration 原始碼）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.retrievers.keyword import MAX_FTS_QUERY_CHARS, MAX_FTS_TERMS, build_fts_query

BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = BACKEND_ROOT / "apps" / "knowledge" / "migrations"
INDEX_NAME = "ix_chunk_content_fts_active"


class TestTermExtraction:
    """規則刻意保守：**寧可棄權也不要亂投**。"""

    def test_an_identifier_inside_a_chinese_question_is_found(self) -> None:
        """2B-2 追因的那一題：`&@*` 對整句回 0 筆，而這裡把詞挑出來單獨查。"""
        assert build_fts_query("向量索引的 ef_search 建議從多少開始調？") == '"ef_search"'

    @pytest.mark.parametrize(
        "text",
        ["ES256 是什麼", "用 pgBackRest 備份", "RPO 是多少", "勞基法第 14 條", "版本 v1.2 有什麼"],
    )
    def test_the_shapes_that_count_as_distinctive(self, text: str) -> None:
        """含數字、含 `_`/`.`/`-`、全大寫、或第一個字元之後還有大寫——這四種都是
        「向量最弱、字面最強」的詞。"""
        assert build_fts_query(text) != ""

    @pytest.mark.parametrize(
        "text",
        [
            "租戶隔離是怎麼做的？",
            "Why is document extraction run inside a subprocess?",
            "The quick brown fox",
            "請假要提前幾天申請",
        ],
    )
    def test_a_question_without_identifiers_abstains(self, text: str) -> None:
        """**棄權是正確答案，不是失敗**。純中文的概念問句與純小寫的英文問句裡沒有字面
        比對幫得上忙的東西；投票的代價是把向量找對的答案擠下去（2B-2 實測 −0.04）。

        句首大寫的普通英文單字（`Why`、`The`）也不算——不然每個英文問句都會投票。
        """
        assert build_fts_query(text) == ""

    def test_terms_are_joined_with_or_in_original_order(self) -> None:
        """OR 而不是 AND：一句話裡的識別符不一定出現在同一段（`RPO` 與 `PITR` 可能
        分屬兩段），AND 會讓那種問句一筆都查不到。"""
        assert build_fts_query("What is the RPO and which tool provides PITR?") == (
            '"RPO" OR "PITR"'
        )

    def test_duplicates_collapse(self) -> None:
        assert build_fts_query("ES256 ES256 ES256") == '"ES256"'

    def test_every_term_is_quoted(self) -> None:
        """每個詞用雙引號包住：`-` 之類的字元在 `&@~` 的語法裡是運算子，包起來之後
        才是字面。詞本身只由 `[A-Za-z0-9_.-]` 組成，所以引號內不可能再有引號。"""
        assert build_fts_query("查 v1.2-beta 的說明") == '"v1.2-beta"'

    def test_a_blank_query_abstains(self) -> None:
        for text in ("", "   ", "\n\t"):
            assert build_fts_query(text) == ""

    def test_it_stops_at_the_term_cap(self) -> None:
        """貼一整段設定檔進來提問時，不該組出上百個 OR——每個都要掃一次倒排索引。"""
        text = " ".join(f"TOKEN{i}" for i in range(MAX_FTS_TERMS * 3))

        assert build_fts_query(text).count(" OR ") == MAX_FTS_TERMS - 1

    def test_an_overlong_question_is_truncated_before_scanning(self) -> None:
        """上限與 `MAX_TOP_K` 同一類（15 §4.1 的例外條款）：保護 DB 的硬上限，住在
        程式碼裡但要有名字，否則它會以字面常數散進兩三個地方。"""
        text = "字" * MAX_FTS_QUERY_CHARS + " ES256"

        assert build_fts_query(text) == ""


@pytest.fixture(scope="module")
def migration_source() -> str:
    """建立這個索引的那一支 migration 的原始碼。

    以「內容含索引名」去找而不是寫死檔名：migration 的編號會隨其他工作包前進，而寫死
    的檔名在那時只會讓這裡以「檔案不存在」失敗，訊息完全不指向真正的問題。
    """
    sources = [
        path.read_text(encoding="utf-8")
        for path in sorted(MIGRATIONS.glob("0*.py"))
        if INDEX_NAME in path.read_text(encoding="utf-8")
    ]
    assert sources, f"沒有任何 migration 建立 {INDEX_NAME}"
    assert len(sources) == 1, f"{INDEX_NAME} 出現在多個 migration 裡"
    return sources[0]


class TestIndexMigration:
    """索引宣告本身。**四個屬性錯了都不會有錯誤訊息**，只會讓檢索悄悄退化或整條 500。"""

    def test_it_is_a_pgroonga_index(self, migration_source: str) -> None:
        assert "USING pgroonga" in migration_source

    def test_it_indexes_tenant_and_kb_alongside_content(self, migration_source: str) -> None:
        """索引少了那兩個欄位的症狀不是變慢，是**每次全文檢索都 500**：planner 會改用
        既有的 btree 去掃，把 `&@*` 當成 runtime filter，而 similar search 只在 index
        scan 下可用（2B-1 實作時實測）。"""
        assert "(tenant_id, kb_id, content)" in migration_source

    def test_it_is_partial_on_active_chunks(self, migration_source: str) -> None:
        """`WHERE superseded = false`：少了它，索引會把每次 re-ingest 的歷史版本都收
        進去，大小隨重跑次數線性成長——而查詢結果完全正確，所以沒有人會發現。

        它同時是查詢那一側的約束（見 `test_fts_retrieval.py`）：partial index 只有在
        查詢也帶著同一個條件時才用得到。
        """
        assert "superseded = false" in migration_source

    def test_it_is_created_concurrently_outside_a_transaction(self, migration_source: str) -> None:
        """`CREATE INDEX CONCURRENTLY` + `atomic = False`（鐵則 7：大表索引必用）。

        兩者缺一不可：`CONCURRENTLY` 不能在交易裡跑，而 Django 的 migration 預設包在
        交易內——只寫前者的話，migration 會在正式庫上以「CREATE INDEX CONCURRENTLY
        cannot run inside a transaction block」失敗，而本機小表可能剛好沒事。
        """
        assert "CONCURRENTLY" in migration_source
        assert "atomic = False" in migration_source

    def test_it_can_be_rolled_back(self, migration_source: str) -> None:
        """reverse 要能把索引刪掉，否則 rollback 之後再前滾會撞「已存在」。"""
        assert "DROP INDEX" in migration_source

"""驗收：全文檢索的查詢前處理與索引宣告（13 §4 工作包 2B-1；06 §3.1 的 FTS 一路）。

**這一層決定「問句怎麼變成 PGroonga 看得懂的東西」**，而 2B-1 的實測（開工前 spike，
2026-08-23）已經把可行的路縮到一條：

- `content &@ '<整句中文問句>'` → **0 筆**：PGroonga 把整句斷成 bigram 後**全部
  AND**，而一段話不可能同時包含問句的每一個字組。
- `content &@~ '<整句>'` → 同樣 0 筆，而且 `(`、`-`、`"` 是查詢語法的運算子：一個
  括號就讓結果變空，**且不報錯**。
- `content &@~ '密碼 OR 鎖住'` → 有效，但要我們自己把中文斷成詞；這個 build 沒有
  `pgroonga_tokenize`，Python 這側也沒有斷詞器。
- `content &@* '<整句>'` → **正中**；短詞、英文問句、含語法字元的問句都正常，不相關
  的查詢與空白回空。

因此 FTS 用 **`&@*`（similar search）**：它自己從查詢文字裡挑代表性的詞，不需要我們斷詞，
也**不需要跳脫**——那些字元對它是普通文字而不是語法。本檔的斷言因此刻意寫成「不做跳脫」，
免得日後有人「順手補上」escape 而讓查詢字面上多出反斜線。

`&@~`（查詢語法）沒有被刪掉的理由：2B-2 之後若要支援「必須包含某詞」的進階查詢，那條路
才走得通。現在不做。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.retrievers.keyword import MAX_FTS_QUERY_CHARS, normalise_fts_query

BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = BACKEND_ROOT / "apps" / "knowledge" / "migrations"
INDEX_NAME = "ix_chunk_content_fts_active"


class TestNormalise:
    def test_it_trims_but_keeps_the_sentence_intact(self) -> None:
        """整句原樣送進 `&@*`——它要的就是一段自然語言，不是關鍵字清單。"""
        assert normalise_fts_query("  密碼連續打錯幾次會被鎖住？  ") == "密碼連續打錯幾次會被鎖住？"

    def test_query_syntax_characters_are_not_escaped(self) -> None:
        """**不跳脫**（見模組 docstring）：`&@*` 把它們當普通文字。

        補上 escape 的話，查詢字串裡會多出字面上的反斜線，而那些反斜線會被當成要比對的
        字元——結果是「問句裡有括號的那幾題突然都查不到」，而且沒有任何錯誤。
        """
        text = '(密碼) -鎖住 "引號"'

        assert normalise_fts_query(text) == text

    def test_a_blank_query_becomes_empty(self) -> None:
        """空查詢由呼叫端擋掉，不准往下走：PGroonga 對空字串回空集合，而那與「真的沒有
        相關內容」在結果上一模一樣。"""
        for text in ("", "   ", "\n\t"):
            assert normalise_fts_query(text) == ""

    def test_an_overlong_query_is_truncated(self) -> None:
        """similar search 的成本隨查詢長度上升，而查詢字串的來源是使用者輸入
        （`MessageCreateIn.content` 上限 4,000 字）。截在這裡，DB 那側才有上界。
        """
        long_text = "字" * (MAX_FTS_QUERY_CHARS + 500)

        assert len(normalise_fts_query(long_text)) == MAX_FTS_QUERY_CHARS

    def test_the_cap_is_not_a_secret_number(self) -> None:
        """上限是保護 DB 的硬上限，與 `MAX_TOP_K` 同一類（15 §4.1 的例外條款）：住在
        程式碼裡、不進可調參數區，但要有名字，否則它會以字面常數散進兩三個地方。"""
        assert MAX_FTS_QUERY_CHARS >= 200


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

"""chunks 的中文全文檢索索引（05 §5.3 的 pgroonga 決策、06 §3.1 的 FTS 一路，2B-1）。

05 §5.3 原本寫的是 `chunks.content_tsv`（一個 generated 欄位）。**不需要那個欄位**：
pgroonga 直接在 `content` 上建索引，而多一個 tsvector 欄位等於把同一份文字存兩次，
每次 re-ingest 都要重算。1B-1 的 model docstring 說「先寫 query pattern 再開 index」
（05 §4 原則），2B-1 就是那個 query pattern 出現的時候。

**索引為什麼是多欄位 `(tenant_id, kb_id, content)` 而不是只有 `content`**（2B-1 實作時
發現，2026-08-23）：只索引 `content` 的話，planner 會挑既有的
`ix_chunk_tenant_kb_active`（btree，服務 tenant + kb 兩個等值條件）去掃，再把
`content &@* '...'` 當成 runtime filter——而 `&@*`（similar search）**只在 index scan
下可用**，PGroonga 會直接拋 `similar search available only in index scan`。症狀是每一次
全文檢索都 500，且錯誤訊息完全不提「你少建了一個索引」。把租戶與 KB 一起收進 pgroonga
索引之後，同一個索引服務三個條件（uuid 等值也進得了 Index Cond），planner 沒有更便宜
的路可挑。

四個屬性各自擋一種沉默的退化：

1. **`USING pgroonga`**：選它的理由是免自訂詞典的中日韓斷詞（05 §5.3）。換成一般的
   tsvector + simple 設定不會報錯，只會讓中文查詢的召回掉到接近零。
1b. **欄位順序 `(tenant_id, kb_id, content)`**：等值條件在前、全文在後（見上方說明）。
2. **`WHERE superseded = false`（partial）**：少了它，索引會把每次 re-ingest 的歷史
   版本都收進去——大小隨重跑次數線性成長，而查詢結果完全正確，所以沒有人會發現。
   它同時是**查詢那一側的約束**：partial index 只有在查詢也帶著同一個條件時才用得到，
   而用不到的症狀是整表掃描 + `pgroonga_score()` 安靜地回 0.0（2B-1 spike 實測）。
3. **`CONCURRENTLY` + `atomic = False`**（鐵則 7：大表索引必用）：兩者缺一不可——
   `CONCURRENTLY` 不能在交易裡跑，而 Django 的 migration 預設包在交易內。只寫前者的話
   會以「CREATE INDEX CONCURRENTLY cannot run inside a transaction block」失敗，而本機
   小表可能剛好沒事（先跑到的是別的路徑）。

**`CONCURRENTLY` 失敗會留下 INVALID 索引**（PostgreSQL 的行為，不是我們的 bug）：它
存在、但不會被查詢使用，而 `pg_indexes` 照樣列得出來。恢復方式是先
`DROP INDEX CONCURRENTLY IF EXISTS ix_chunk_content_fts_active;` 再重跑本 migration。
`IF NOT EXISTS` 在這裡是為了讓那次重跑不必先手動清乾淨。
"""

from __future__ import annotations

from django.db import migrations

INDEX_NAME = "ix_chunk_content_fts_active"

CREATE_INDEX = f"""
    CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME}
        ON knowledge_chunk USING pgroonga (tenant_id, kb_id, content)
        WHERE superseded = false;
"""

DROP_INDEX = f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME};"


class Migration(migrations.Migration):
    # 見模組 docstring 第 3 點：CONCURRENTLY 的前提。
    atomic = False

    dependencies = [("knowledge", "0005_document_uploaded_by")]

    operations = [
        migrations.RunSQL(sql=CREATE_INDEX, reverse_sql=DROP_INDEX),
    ]

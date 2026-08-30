"""向量欄位 halfvec(1536) → halfvec(1024)，並清空既有向量（W1，`BAAI/bge-m3`）。

**這是一次不可逆的全庫重建**（2026-08-30 人類裁決）。06 §2.2 的四步重嵌入靠
`UNIQUE(chunk, model, embedding_version)` 讓新舊兩版並存、舊版持續服務、隨時可回退
——那個流程是為**換模型**寫的，而它默默假設了維度不變：halfvec 是固定寬度的欄位型別，
1024 與 1536 塞不進同一欄。裁決是不加第二欄、不開新表，直接改欄位並清空既有向量。

**先 TRUNCATE 再 ALTER，兩者的順序不能反**：`ALTER COLUMN ... TYPE halfvec(1024)` 對
既有列會逐列轉型，而每一列都是 1536 維——它會失敗。真正麻煩的不是失敗本身，是它
**只在有資料的環境失敗**：空的 CI 資料庫一路綠燈，開發機與正式環境紅，於是問題看起來
像機器的問題。

**用 TRUNCATE 而不是 DELETE**：這張表開著 FORCE RLS（`0004_embedding_rls`），而
migration 沒有租戶脈絡——`DELETE` 會被 policy 濾成 0 列並且**成功回傳**，接著下一步的
ALTER 才失敗，而錯誤訊息講的是型別。RLS 完全不作用於 TRUNCATE（它看的是權限，不是
列），所以那正是這裡要的語意。

**reverse 只還原欄位寬度，還原不了向量**：那些是 provider 算出來的，不在資料庫裡。
回滾之後檢索會查不到任何東西（不會報錯，只是零筆），要重跑一次重建才會恢復。

**跑完之後每個 KB 都要重建**：chunk 都還在，缺的只有向量，走 2B-6 的 KB reindex
（`POST /knowledge-bases/{id}/reindex`）。在那之前，文件狀態仍是 `ready` 而檢索查不到
它們——這是這次改動唯一「看起來正常但其實不對」的窗口，發布時要一起處理。
"""

from __future__ import annotations

import pgvector.django.halfvec
from django.db import migrations

TABLE = "knowledge_embedding"

# reverse 不需要對稱地清空：回滾的目的地是舊寬度，而那時表已經是空的
# （1024 的向量同樣塞不進 halfvec(1536)，所以順序仍然是先清後改）。
TRUNCATE = f"TRUNCATE TABLE {TABLE};"


class Migration(migrations.Migration):
    dependencies = [("knowledge", "0008_kb_reindex_job_rls")]

    operations = [
        migrations.RunSQL(sql=TRUNCATE, reverse_sql=TRUNCATE),
        migrations.AlterField(
            model_name="embedding",
            name="vector",
            field=pgvector.django.halfvec.HalfVectorField(dimensions=1024),
        ),
    ]

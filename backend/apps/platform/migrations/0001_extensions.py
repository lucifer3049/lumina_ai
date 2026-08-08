"""PostgreSQL extension：pgvector 與 pgroonga（05 §3.2、§5.3）。

**現況（1A-1 角色拆分後）**：開發環境的 extension 由
`docker/postgres/initdb.d/20-extensions.sh` 以 superuser 在 initdb 建立，並且
對 `template1` 也建一次，因此 pytest 另建的 `test_<db>` 天生就帶著兩者。本檔於是
在開發與 CI 環境**實際上是 no-op**——`CreateExtension` 會先查 `pg_extension`，
已存在就整段跳過。

**保留本檔的理由**：`CREATE EXTENSION` 需要 superuser（vector 與 pgroonga 都不是
trusted extension），而 migration 自 1A 起以非 superuser 的 schema owner 執行
（13 §3.1）。這裡是「本專案需要哪些 extension」的單一宣告處，也涵蓋不經上述腳本
建立的資料庫；在那種環境下，缺 extension 會以明確的權限錯誤現形，而不是等到某條
用 halfvec 的 SQL 才炸。正式環境的建立方式依 05 §5.1，由具權限的維運流程處理。
"""

from django.contrib.postgres.operations import CreateExtension
from django.db import migrations


class Migration(migrations.Migration):
    initial = True

    dependencies: list[tuple[str, str]] = []

    operations = [
        CreateExtension("vector"),
        CreateExtension("pgroonga"),
    ]

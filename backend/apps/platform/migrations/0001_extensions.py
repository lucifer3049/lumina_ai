"""PostgreSQL extension：pgvector 與 pgroonga（05 §3.2、§5.3）。

**為什麼 extension 走 migration 而不是 image 的 initdb.d**：initdb.d 的腳本只在
資料卷首次初始化時執行一次。pytest 會另建 `test_<db>`（從 template1 複製），
那個資料庫不會經過 initdb.d——extension 於是「本機的 lumina 有、測試庫沒有」，
症狀是本機綠、CI 紅，而且錯誤指向的是使用 halfvec 的那條 SQL，不是缺 extension。
放進 migration 之後，任何被 Django 建起來的資料庫都必然帶著這兩個 extension。

**權限**：`CREATE EXTENSION` 需要 superuser（vector 與 pgroonga 都非 trusted）。
開發用的 compose role 本身是 superuser；正式環境依 05 §5.1，migration 由具權限的
維運角色執行，應用連線用的非 superuser 角色不需要（也不應該有）這個權限。
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

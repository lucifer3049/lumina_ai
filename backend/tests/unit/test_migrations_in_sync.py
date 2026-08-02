"""驗收：model 與 migration 沒有漂移（05 §5.6：CI 檢查 `makemigrations --check`）。

漏生 migration 的症狀特別壞：本機開發者的資料庫是手動改過、或早就有那個欄位的，
所以測試全綠；到了 CI 或別人的機器上，migration 建出來的表少一欄，
錯誤卻出現在完全不相干的查詢裡。

**為什麼不直接呼叫 `call_command("makemigrations", check=True)`**：那條路會去連資料庫
（比對 migration 歷史一致性），於是這個純粹比對「model 定義 vs migration 檔案」的檢查
變成需要 DB 才能跑，unit 層跑不了、CI 也得先起服務。這裡改用 autodetector：
`MigrationLoader(None)` 明確不帶連線，整段完全在記憶體裡完成。
"""

from __future__ import annotations

from django.apps import apps
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
from django.db.migrations.state import ProjectState


def test_no_missing_migrations() -> None:
    """有未生成的 model 變更就失敗。修法：`python manage.py makemigrations` 後提交。"""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    autodetector = MigrationAutodetector(
        loader.project_state(),
        ProjectState.from_apps(apps),
        NonInteractiveMigrationQuestioner(specified_apps=set(), dry_run=True),
    )
    changes = autodetector.changes(graph=loader.graph)

    assert not changes, "偵測到未生成的 migration：" + ", ".join(
        f"{app}（{len(migrations)} 項變更）" for app, migrations in sorted(changes.items())
    )

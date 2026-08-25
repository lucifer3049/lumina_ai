"""評測租戶的知識庫清理（`make eval-clean`；二次架構審計 F-12）。

離線檢索評測（`scripts/eval_retrieval.py`）會在評測租戶底下建 `eval-{題組}` 知識庫並
灌進整份語料——2B-4 時是 1,499 個 chunk 加同樣數量的向量，而它們**在開發庫裡常駐**：
評測跑完沒有人清，下一次 `make up` 它們還在。這不是正確性問題（評測租戶與其他租戶
完全隔離），是開發環境衛生：一個從來沒有人主動建立的租戶佔著最大的一張表。

**刻意不刪租戶本身**：租戶是計費與隔離的單位，而下一次評測還要用它（`resolve_kb`
的註解也記著「租戶不建、知識庫才建」）。這支指令刪的是它的**工作區**。

**走 `DeletedKnowledgePurgeService` 而不是自己刪**：級聯順序（向量 → chunk →
etl_job → 物件 → 文件 → KB）已經在那裡定案且有測試，第二份實作遲早會漏一步，而
漏掉的症狀是「每次跑都被 FK 擋下」或更糟的孤兒資料。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import TenantDirectoryRepository
from repositories.knowledge import KnowledgeBaseRepository
from services.knowledge.purge import DeletedKnowledgePurgeService

# 評測知識庫的命名前綴（`scripts/eval_retrieval.py` 的 `resolve_kb`：`eval-{題組}`）。
# 只刪這個前綴的，是為了讓「有人在評測租戶裡放了別的東西」不會被順手清掉。
_EVAL_KB_PREFIX = "eval-"


class Command(BaseCommand):
    help = "刪除評測租戶底下的 eval-* 知識庫及其全部文件、chunk、向量與物件"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--tenant", required=True, help="評測租戶的 slug（如 lumina-eval）")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只列出會被刪的知識庫，不動任何資料",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        slug = str(options["tenant"])
        tenant_id = TenantDirectoryRepository().get_active_tenant_id(slug)
        if tenant_id is None:
            raise CommandError(f"找不到 active 的租戶 {slug!r}")

        targets = self._eval_knowledge_bases(tenant_id)
        if not targets:
            self.stdout.write(f"{slug}：沒有 {_EVAL_KB_PREFIX}* 知識庫，無事可做")
            return

        names = "、".join(name for _, name in targets)
        if options["dry_run"]:
            self.stdout.write(f"[dry-run] 會刪除 {len(targets)} 個知識庫：{names}")
            return

        with tenant_context(tenant_id), unit_of_work():
            repository = KnowledgeBaseRepository()
            for kb_id, _ in targets:
                repository.soft_delete(kb_id)

        # **保留窗開到未來**：這些 KB 是上面那一行剛軟刪的，用正常的 30 天窗一個都
        # 掃不到。這正是 `deleted_before` 存在的理由，也是它只給維運指令用的理由。
        counts = DeletedKnowledgePurgeService().purge_for_tenant(
            tenant_id, deleted_before=timezone.now() + timedelta(minutes=1)
        )
        self.stdout.write(
            f"{slug}：刪除 {names}——"
            f"知識庫 {counts.knowledge_bases}、文件 {counts.documents}、"
            f"chunk {counts.chunks}、物件 {counts.objects}"
        )

    def _eval_knowledge_bases(self, tenant_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
        with tenant_context(tenant_id), unit_of_work():
            return [
                (uuid.UUID(str(kb.id)), str(kb.name))
                for kb in KnowledgeBaseRepository().list_all()
                if str(kb.name).startswith(_EVAL_KB_PREFIX)
            ]

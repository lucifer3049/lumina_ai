"""``manage.py requeue_stuck_documents`` —— 把停住的文件重新排進佇列。

**存在的理由**：送任務是 best-effort。broker 在上傳的那一刻掛掉時，
`enqueue_ingestion` 記一行 log 就走（讓使用者的上傳因為背景設施而失敗，是把一個
可回復的問題變成不可回復的），而代價是那份文件停在 ``uploaded``——**沒有任何訊息
存在，所以不會有人重試**。ETL 跑完後排 embedding 時同理，那時停在 ``chunked``。

兩種狀況在 API 側都完全看不出來：上傳回 201、佇列深度正常、worker 的 log 乾淨。
只有那份文件永遠不會前進。

**為什麼要指定租戶**：全租戶掃描要能列舉租戶，而 ``identity_tenant`` 自己就有 RLS
（``id = current_tenant``），列舉它需要 BYPASSRLS 角色——13 §3.1 v1.7 明文決定那個
角色要等 2A 才建（提前建一個「沒人用但看得到全部租戶資料」的角色，風險是純增加
的）。自動化的全域掃描（Celery Beat）同樣排 2A；在那之前這支指令是恢復入口，可由
外部排程逐租戶呼叫。

實際邏輯在 `DocumentService.requeue_stuck`，與未來的 API／Beat 走同一條路徑。
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from repositories.identity import TenantDirectoryRepository
from services.knowledge.documents import DocumentService

# 預設 15 分鐘：``uploaded`` 與 ``chunked`` 是正常的過渡狀態，一份大 PDF 在 ETL 裡
# 待上幾分鐘是常態。門檻太短會把**正在被處理**的文件再排一次——冪等保證那不會弄壞
# 資料，但 embedding 是真的錢，而重複的訊息會讓佇列在最需要吞吐時變成兩倍長。
_DEFAULT_STALE_MINUTES = 15


class Command(BaseCommand):
    help = "把停在 uploaded / chunked 的文件重新排進佇列（broker 曾經送不出去時的恢復入口）"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--tenant", required=True, help="租戶 slug（見本檔 docstring）")
        parser.add_argument(
            "--stale-after-minutes",
            type=int,
            default=_DEFAULT_STALE_MINUTES,
            help=f"多久沒有動靜才算停住（預設 {_DEFAULT_STALE_MINUTES} 分鐘）",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只列出會被重排的文件，不送任何訊息",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        slug = options["tenant"]
        tenant_id = TenantDirectoryRepository().get_active_tenant_id(slug)
        if tenant_id is None:
            raise CommandError(f"找不到啟用中的租戶：{slug}")

        stale_after = int(options["stale_after_minutes"])
        if stale_after < 1:
            # 0 會把剛上傳、正在被 worker 處理的文件一起排下去（見 _DEFAULT_STALE_MINUTES）。
            raise CommandError("--stale-after-minutes 至少要是 1")

        requeued = DocumentService().requeue_stuck(
            tenant_id,
            stale_after_minutes=stale_after,
            dry_run=bool(options["dry_run"]),
        )

        if not requeued:
            self.stdout.write(f"租戶 {slug}：沒有停住超過 {stale_after} 分鐘的文件")
            return

        prefix = "將重排" if options["dry_run"] else "已重排"
        for document_id, status in requeued:
            self.stdout.write(f"{prefix} {document_id}（{status}）")
        self.stdout.write(f"租戶 {slug}：{prefix} {len(requeued)} 份文件")

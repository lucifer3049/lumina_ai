"""產生壓測資料。

**租戶數量會直接影響結論，別用預設的 2 個租戶下判斷。**
上一輪 spike 只 seed 了 2 個租戶，目標租戶佔全表 50%——這種比例下即使索引
沒被用到，全表掃描也很快，測不出問題。500 個租戶時每個只佔 0.2%，索引用不用
得到的差距才會現形。預設值因此設為 50 個租戶 × 2000 筆。

租戶 id 用 uuid5 從固定 namespace 推導，所以每次 seed 結果一致、可複現；
同時寫出 ``loadtest/tenants.json`` 供 locust 讀取，避免兩邊各自硬編。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.spike.models import SpikeItem

# 固定 namespace：讓 tenant id 可複現
TENANT_NS = uuid.UUID("00000000-0000-5000-8000-000000000001")
TENANTS_JSON = Path(__file__).resolve().parents[4] / "loadtest" / "tenants.json"


def tenant_id_for(index: int) -> uuid.UUID:
    return uuid.uuid5(TENANT_NS, f"spike-tenant-{index}")


class Command(BaseCommand):
    help = "產生 spike 壓測資料（可重複執行，會先清空 spike_item）"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--tenants", type=int, default=50)
        parser.add_argument("--per-tenant", type=int, default=2000)
        parser.add_argument("--batch", type=int, default=5000)

    def handle(self, *args: Any, **options: Any) -> None:
        n_tenants: int = options["tenants"]
        per_tenant: int = options["per_tenant"]
        batch: int = options["batch"]

        self.stdout.write("清空 spike_item …")
        SpikeItem.objects.all().delete()

        tenant_ids = [tenant_id_for(i) for i in range(n_tenants)]
        total = n_tenants * per_tenant
        self.stdout.write(f"寫入 {n_tenants} 租戶 × {per_tenant} 筆 = {total} 筆 …")

        buffer: list[SpikeItem] = []
        written = 0
        for t_index, tenant_id in enumerate(tenant_ids):
            for i in range(per_tenant):
                buffer.append(SpikeItem(tenant_id=tenant_id, title=f"t{t_index}-item-{i}"))
                if len(buffer) >= batch:
                    with transaction.atomic():
                        SpikeItem.objects.bulk_create(buffer)
                    written += len(buffer)
                    buffer.clear()
                    self.stdout.write(f"  {written}/{total}", ending="\r")
        if buffer:
            with transaction.atomic():
                SpikeItem.objects.bulk_create(buffer)
            written += len(buffer)

        TENANTS_JSON.parent.mkdir(parents=True, exist_ok=True)
        TENANTS_JSON.write_text(
            json.dumps([str(t) for t in tenant_ids], indent=2),
            encoding="utf-8",
        )

        self.stdout.write(self.style.SUCCESS(f"\n完成：{written} 筆"))
        self.stdout.write(f"租戶清單已寫入 {TENANTS_JSON}")

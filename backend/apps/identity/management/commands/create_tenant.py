"""``manage.py create_tenant`` —— 租戶開通（13 §4：平台管理面用 CLI / Admin 頂替）。

**為什麼是 CLI 而不是只靠 Django Admin**：1A-5 的 smoke suite 要在 CI 裡自動建
租戶，Admin 點不了。而且 CLI 能把「建租戶必須同時建 Owner」這條規則寫死——
手動建的話很容易產生一個沒有任何使用者的空租戶，然後沒有人登得進去。

實際邏輯全在 `TenantService`：這支指令只負責解析參數與輸出，與 API 端點走同一
條程式碼路徑（否則兩條路會漂，而 CLI 建出來的租戶「看起來一樣但就是不能用」）。
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from core.exceptions import ConflictError, DomainError
from services.identity.tenants import TenantService


class Command(BaseCommand):
    help = "建立租戶與它的第一個 Owner 使用者"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--name", required=True, help="租戶顯示名稱")
        parser.add_argument("--slug", required=True, help="登入用的公司代號（唯一）")
        parser.add_argument("--owner-email", required=True)
        parser.add_argument("--owner-password", required=True)
        parser.add_argument(
            "--exist-ok",
            action="store_true",
            help="slug 已存在時視為成功（給可重複執行的腳本用，如 make demo-tenant）",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            created = TenantService().create_tenant(
                name=options["name"],
                slug=options["slug"],
                owner_email=options["owner_email"],
                owner_password=options["owner_password"],
            )
        except ConflictError as exc:
            # --exist-ok：開通腳本要能無腦重跑。「已存在」與「真正的衝突」在這裡
            # 無法再細分（slug 唯一鍵就是存在的定義），所以整類視為成功即可。
            if options["exist_ok"]:
                self.stdout.write(f"租戶 {options['slug']} 已存在，略過（--exist-ok）")
                return
            raise CommandError(str(exc)) from exc
        except DomainError as exc:
            # 轉成 CommandError：Django 會印成一行錯誤並回非零離開碼，
            # 而不是吐一整串 traceback（那在 CI log 裡沒有任何額外資訊）。
            raise CommandError(str(exc)) from exc

        # **不印密碼**（鐵則 9）：CLI 輸出會被貼進工單、CI log、聊天室，
        # 那是密碼最常見的外流路徑之一。
        self.stdout.write(
            f"已建立租戶 {created.slug}（id={created.tenant_id}）"
            f"與 Owner {options['owner_email']}（id={created.owner_user_id}）"
        )

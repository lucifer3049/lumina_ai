"""`/settings` 端點（09 §2.6）——租戶級設定，三層覆寫的中間層（2C-1）。

**與 `/tenants/current` 分開**是 09 的形狀：那一支管的是租戶的身分（名稱、方案、
狀態），這一支管的是**行為**（檢索與切塊參數、配額覆寫，日後還有 provider 憑證）。
混在一起的話，「改個名字」與「改整個租戶的檢索參數」會共用同一個權限與同一筆稽核。

鐵則 3（Controller 三行原則）：解析請求 → 呼叫一個 Service 方法 → 回傳。

**兩個動詞都是 `tenant:admin`**（09 §2.6）。讀也是 admin 的理由：這一欄日後會住
provider 憑證（2C-2），而權限一旦鬆過就很難再收緊——那時要改的不只是這裡，還有每
一個已經拿到 `tenant:read` 的整合方。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from api.dependencies.auth import Principal
from api.dependencies.permissions import RequireScope
from api.schemas.problem import ERROR_RESPONSES
from api.schemas.settings import TenantSettingsOut, TenantSettingsUpdateIn
from core.db import run_orm
from services.platform.settings import TenantSettingsService

router = APIRouter(tags=["settings"], responses=ERROR_RESPONSES)
_service = TenantSettingsService()


@router.get("/settings", operation_id="settings_get")
async def get_settings(
    principal: Annotated[Principal, Depends(RequireScope("tenant:admin"))],
) -> TenantSettingsOut:
    """目前的租戶層覆寫。

    **讀得回來很重要**：只能寫不能讀的話，設定畫面要嘛自己記一份（會與 DB 漂），
    要嘛每次顯示空白——而空白與「沒有覆寫」在畫面上長得一樣（同 2B-5 的 KB `config`）。
    """
    view = await run_orm(_service.get, principal.tenant_id)
    return TenantSettingsOut(settings=view.settings)


@router.patch("/settings", operation_id="settings_update")
async def update_settings(
    payload: TenantSettingsUpdateIn,
    principal: Annotated[Principal, Depends(RequireScope("tenant:admin"))],
) -> TenantSettingsOut:
    """逐區的部分更新（沒送到的區塊原封不動；``{}`` 是清空該區）。

    驗證與錯誤形狀在 Service（`settings.<區>.<鍵>` 逐欄位 422）——與 KB config 走同
    一份參數宣告，兩套的話同一個參數會在一邊填得進去、另一邊填不進去。
    """
    view = await run_orm(_service.update, principal.tenant_id, payload.settings)
    return TenantSettingsOut(settings=view.settings)

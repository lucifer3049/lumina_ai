"""Spike 端點 —— 壓測打的目標。

鐵則 3（Controller 三行原則）：解析請求 → 呼叫一個 Service 方法 → 回傳。
本檔不得出現任何 ORM、查詢或業務判斷。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from api.schemas.problem import ERROR_RESPONSES
from api.schemas.spike import SpikeItemOut
from core.db import orm_runtime_knobs
from services.spike import SpikeService

# responses 掛在 router 而非逐端點：逐端點宣告必然會漏，而漏掉時 OpenAPI 只是
# 少一段、不會報錯。09 §4 要求錯誤契約進文件供前端 codegen 使用（鐵則 10）。
router = APIRouter(tags=["spike"], responses=ERROR_RESPONSES)
_service = SpikeService()


@router.get("/spike/items", operation_id="spike_list_items")
async def list_items(limit: int = Query(20, ge=1, le=100)) -> list[SpikeItemOut]:
    """當前租戶最新 N 筆——B 組壓測的主要目標端點。

    dict → DTO 的組裝是 controller 的 response shaping 職責，不是業務邏輯，
    所以留在這裡（鐵則 3）。反過來讓 service 回 DTO 會要求 services/ import
    api/，方向錯了。

    ⚠️ **量測基準線註記**：這層 Pydantic 組裝在熱路徑上，與此改動之前的 B 組
    數據**不可直接比較**；要沿用舊數字必須重跑基準線。
    """
    return [SpikeItemOut(**row) for row in await _service.latest_items(limit)]


@router.get("/spike/healthz", operation_id="spike_healthz")
async def healthz() -> dict[str, Any]:
    """回報壓測旋鈕值。

    壓測時先打這支確認「你以為設定的值」和「真正生效的值」一致——上一輪
    量測不可信的教訓之一就是設定沒生效卻照樣記數據。

    內容一律由 ``core/db.py`` 的 :func:`orm_runtime_knobs` 決定，本檔不自行組。
    前一版在這裡 ``from django.conf import settings`` 並回報 ``db_host`` /
    ``db_port``，同時違反三條鐵則：controller 讀基礎設施（3）、api 層碰 Django
    組態（2 的精神，import-linter 只擋 repositories/apps 攔不到）、以及把內部拓撲
    經**無認證**端點外流（9）。這支是 repo 裡唯一的 diagnostics endpoint，1A 要建
    正式的 ``/healthz``（11 §4.2）時抄的就是它。
    """
    return orm_runtime_knobs()

"""測試共用 fixture。

**雙租戶是預設，不是選項**（CLAUDE.md 測試規範）：只有一個租戶的測試無法
證明隔離有效——查詢當然只會回傳那個租戶的資料。所以 fixture 一律建兩個租戶
並各自塞資料，隔離斷言才有意義。
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator

import pytest

from apps.spike.models import SpikeItem
from core.tenant import tenant_context

# ── 環境守門（第二道；第一道在 Makefile 頂部）────────────────────────
# 本專案統一在 WSL2 開發。從 Windows 側跑 `uv run pytest` 時，uv 在 pytest 啟動
# **之前**就已把 WSL2 建的 .venv 砍掉重建（跨平台 venv 不相容）——這裡攔不回來，
# 但能把「為什麼測試環境突然壞掉」講清楚，而不是讓人繼續在 Windows 上開發到
# venv 變成殘骸（2026-08-03 實際發生）。CI（ubuntu）與 WSL2 都是 linux，不受影響。
#
# 訊息刻意寫成**純 ASCII 英文**：Windows 主控台預設是本地 codepage（繁中為
# cp950），中文訊息在那裡會整段變成亂碼——而這正是唯一會看到這則訊息的平台。
# 上面的中文註解給讀原始碼的人看，那是在編輯器裡，不受 codepage 影響。
# （2026-08-03 實測：原本的中文版在 Windows console 呈現為「���M�ײΤ@...」。）
if sys.platform == "win32":
    raise pytest.UsageError(
        "This project must be developed inside WSL2; run the tests there, not from Windows. "
        "The uv call you just made has already rebuilt backend/.venv for Windows. "
        "Back in WSL2 the first command will rebuild it again (plain `uv sync`, no data loss)."
    )

TENANT_A = uuid.UUID("11111111-1111-5111-8111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-5222-8222-222222222222")


@pytest.fixture
def two_tenants(transactional_db: object) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """建立兩個租戶各 3 筆資料。

    用 ``transactional_db`` 而非 ``db``：run_orm 把查詢送到另一條執行緒，
    pytest-django 預設的交易包裹在那頭看不到，測試會以假失敗誤導人。
    """
    SpikeItem.objects.bulk_create(
        [SpikeItem(tenant_id=TENANT_A, title=f"a-{i}") for i in range(3)]
        + [SpikeItem(tenant_id=TENANT_B, title=f"b-{i}") for i in range(3)]
    )
    yield TENANT_A, TENANT_B


@pytest.fixture
def as_tenant_a() -> Iterator[uuid.UUID]:
    with tenant_context(TENANT_A) as tid:
        yield tid

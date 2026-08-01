"""B 組壓測 —— ADR-001 橋接吞吐量測。

用法（先 ``make up`` → ``make migrate`` → ``make seed`` → ``make api``）：

    make loadtest          # 開 web UI（http://localhost:8089）
    make loadtest-headless # 無頭跑 60 秒直接吐數字

⚠️ **量測環境警告**：上一輪的數據不可信，因為壓測工具與受測系統跑在同一台
機器上，同設定不同次執行變異達 ±35%（565–877 rps）。這種雜訊足以吃掉一個真實
的效能差異。判讀規則：

- 差距 **> 50%**（例如 CONN_MAX_AGE 0 vs 300 的 2.4 倍）→ 雜訊蓋不掉，可信。
- 差距 **< 50%**（例如 threadpool 12 vs 24）→ **在本機環境下分辨不出來**，
  不要據此下結論。

要得到可信的邊界值（例如 p95 < 300ms 的達標判定），必須把 locust 移到另一台
機器再測。11 §1.4 的 baseline 在此之前不應被視為有效。
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from locust import HttpUser, between, events, task

_TENANTS_JSON = Path(__file__).resolve().parent / "tenants.json"
_TENANTS: list[str] = []


@events.test_start.add_listener
def _load_tenants(**_: Any) -> None:
    """從 seed 產生的清單讀租戶 id，避免與 seed 指令各自硬編而漂掉。"""
    if not _TENANTS_JSON.exists():
        raise RuntimeError(f"找不到 {_TENANTS_JSON}——請先執行 `make seed` 產生壓測資料與租戶清單")
    _TENANTS.extend(json.loads(_TENANTS_JSON.read_text(encoding="utf-8")))
    print(f"[loadtest] 載入 {len(_TENANTS)} 個租戶")


class SpikeUser(HttpUser):
    # 不加 think time：這裡要量的是系統上限，不是模擬真人節奏。
    wait_time = between(0, 0)

    @task
    def list_items(self) -> None:
        tenant = random.choice(_TENANTS)  # noqa: S311 - 壓測取樣，非密碼學用途
        self.client.get(
            "/api/v1/spike/items?limit=20",
            headers={"X-Tenant-Id": tenant},
            name="/api/v1/spike/items",
        )

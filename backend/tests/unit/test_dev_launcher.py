"""驗收：一鍵啟停涵蓋所有必須跑的服務（`scripts/dev.sh` + Makefile）。

**這是「單一來源」那條規則的守門**：啟動什麼指令由 Makefile 決定，怎麼跑由 dev.sh
決定（見該腳本開頭）。兩邊各自演化時的失敗方式很安靜——1B-6 加了 ETL worker，若只
加了 `make worker` 而沒接進 `make start`，`make start` 一樣印成功 banner，上傳一樣
回 201，只是文件永遠停在 `uploaded`：訊息進了佇列而沒有人處理，API 側完全看不出來。

因此這裡逐項對帳：每個服務都要被啟動、停止、列進 status 與 logs，且它的指令是由
Makefile 傳進來的。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEV_SH = (_REPO_ROOT / "scripts" / "dev.sh").read_text(encoding="utf-8")
_MAKEFILE = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")

# 一鍵啟停必須涵蓋的服務 → 它的指令變數名。
SERVICES = {
    "api": "API_CMD",
    "frontend": "FE_CMD",
    "worker": "WORKER_CMD",
}


@pytest.mark.parametrize(("service", "variable"), sorted(SERVICES.items()))
class TestEveryServiceIsManaged:
    def test_it_is_started(self, service: str, variable: str) -> None:
        assert f'start_one {service} "${{{variable}}}"' in _DEV_SH

    def test_it_is_stopped(self, service: str, variable: str) -> None:
        """停不掉的服務會佔著資源到下次重開機——worker 還會繼續消化佇列。"""
        assert f"stop_one {service}" in _DEV_SH

    def test_its_command_comes_from_the_makefile(self, service: str, variable: str) -> None:
        """指令由 Makefile 傳入（單一來源）。

        dev.sh 自帶一份的話，`make dev` / `make worker`（前景）與 `make start`
        （背景）會慢慢漂成兩套參數，而漂掉時的症狀是「背景跑起來的行為跟前景不同」。
        """
        assert f"{variable}=" in _MAKEFILE
        assert f'{variable}="${{{variable}:?' in _DEV_SH


class TestObservability:
    def test_status_lists_every_service(self) -> None:
        listed = _DEV_SH.split("for name in ", 1)[1].split(";", 1)[0].split()

        assert set(listed) == set(SERVICES)

    def test_logs_include_every_service(self) -> None:
        tail_line = next(line for line in _DEV_SH.splitlines() if "tail -f" in line)

        for service in SERVICES:
            assert f"log_file {service}" in tail_line

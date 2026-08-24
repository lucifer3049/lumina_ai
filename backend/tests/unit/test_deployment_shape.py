"""驗收：應用層的部署形狀（`docker/compose.app.yml`；二次架構審計 F-01）。

第一輪審計的 F-01 是「部署形狀不存在」：compose 只有五個資料層服務、`prod.py` 只有
一行 `from .base import *`、全 api/v1 沒有 healthz。這一檔釘住補上來的那份 compose
的幾條不變量——**它們全部是「漂掉不會有任何症狀」的那一類**，而症狀要等到部署當下
（或更糟，等到重啟時弄丟一批回答）才出現。

方法論同 `test_platform_beat.py`：靜態設定的正確性在 unit 驗（解析檔案），行為在
integration 驗。這裡刻意不起容器——那要 Docker，而 CI 的 quality job 沒有。

**五條不變量，各自對應一個真實的失敗**：

1. **`stop_grace_period` ≥ drain 上限**。小於的話，`drain()` 的 30 秒等待會在中途被
   SIGKILL 打斷——那個上限就等於不存在，而進行中的回答直接蒸發，DB 裡那一則永遠是
   `streaming`。這是本檔最重要的一條：兩個數字寫在兩個檔案裡，沒有人會同時想到它們。
2. **三個角色都顯式帶 `DJANGO_SETTINGS_MODULE`**。`config/celery_app.py` 用的是
   `setdefault(..., "config.settings.dev")`（審計 L4），漏帶就是拿 dev 設定跑部署，
   而目前 dev 恰好等同 base，所以完全沒有徵兆。
3. **worker 的佇列與 Makefile 的 `WORKER_CMD` 一致**。少聽一條佇列的症狀是「那條
   佇列的訊息堆著沒人處理」——worker 啟動成功、log 乾淨（1B-6／1C-4 的形狀）。
4. **healthcheck 打 `/readyz` 而不是 `/healthz`**。compose 的 healthcheck 決定
   `depends_on: service_healthy`，那是就緒語意；打 liveness 的話，DB 還沒起來的
   容器會被判定為健康，而依賴它的服務會提早開始。
5. **api 對外埠綁 127.0.0.1**。與 compose.yml 同一條規則：`"8000:8000"` 的簡寫等同
   0.0.0.0，接上公用 wifi 時同網段任何人都打得到，而本機完全不會有徵兆。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from api.background import SHUTDOWN_DRAIN_SECONDS
from tests.unit.test_dev_launcher import _MAKEFILE  # 同一份 Makefile 快照

_COMPOSE_APP = Path(__file__).resolve().parents[3] / "docker" / "compose.app.yml"
_APP_ROLES = ("api", "worker", "beat")


def _compose() -> dict[str, Any]:
    """讀 compose.app.yml。

    **`yaml.safe_load` 就能展開 anchor**（`x-app: &app` + `<<: *app`）：merge key
    是 YAML 1.1 的標準特性，PyYAML 原生支援。不必起 docker 也不必跑
    `docker compose config`——後者要求 .env 存在，而 CI 的 quality job 沒有。
    """
    return dict(yaml.safe_load(_COMPOSE_APP.read_text(encoding="utf-8")))


def _services() -> dict[str, Any]:
    return dict(_compose()["services"])


def _seconds(value: str) -> int:
    """`"35s"` → 35。compose 的 duration 字面值，這裡只需要秒。"""
    match = re.fullmatch(r"(\d+)s", str(value).strip())
    assert match, f"預期形如 '35s' 的秒數，實際是 {value!r}"
    return int(match.group(1))


class TestShutdownContract:
    def test_the_api_grace_period_outlasts_the_drain_deadline(self) -> None:
        """**本檔最重要的一條。**

        `api/background.py` 的 `drain()` 最多等 `SHUTDOWN_DRAIN_SECONDS` 秒讓進行中的
        生成收工。compose 的 `stop_grace_period` 若小於它，等待會在中途被 SIGKILL
        打斷——上限形同虛設，而那些回答直接蒸發（11 §196）。

        兩個數字分別寫在 Python 與 YAML 裡，改任何一邊都不會驚動另一邊。
        """
        grace = _seconds(_services()["api"]["stop_grace_period"])

        assert grace > SHUTDOWN_DRAIN_SECONDS, (
            f"寬限期 {grace}s 不大於 drain 上限 {SHUTDOWN_DRAIN_SECONDS}s"
            "——drain 會被 SIGKILL 打斷，那個上限就不存在了"
        )

    def test_the_worker_waits_long_enough_for_a_task_in_flight(self) -> None:
        """worker 的寬限期要遠大於 api：ETL 的一份大 PDF 可能跑幾分鐘。

        `acks_late` 之下被硬砍的 task 會回到佇列重跑——不是資料損失，但是一次白花的
        解析成本，而那是這套系統裡最貴的一段。
        """
        api_grace = _seconds(_services()["api"]["stop_grace_period"])
        worker_grace = _seconds(_services()["worker"]["stop_grace_period"])

        assert worker_grace > api_grace


class TestEveryRoleIsConfigured:
    def test_all_three_roles_pin_the_settings_module(self) -> None:
        """審計 L4：`config/celery_app.py` 的預設是 `config.settings.dev`。

        漏帶就是拿 dev 設定跑部署，而 dev 目前恰好等同 base——沒有任何徵兆，直到
        有人在 dev.py 加了東西（那個檔案的 docstring 正在討論 `DEBUG`）。
        """
        services = _services()

        for role in _APP_ROLES:
            settings_module = services[role]["environment"]["DJANGO_SETTINGS_MODULE"]
            assert settings_module == "config.settings.prod", (
                f"{role} 的 settings 是 {settings_module}"
            )

    def test_all_three_roles_share_one_image(self) -> None:
        """一個 image 多角色（backend/Dockerfile 檔頭）。三份會漂，而漂掉的那一份
        通常是 worker——症狀是「某些文件永遠停在 parsing」。"""
        images = {_services()[role]["image"] for role in _APP_ROLES}

        assert len(images) == 1, f"三個角色用了不同的 image：{images}"

    def test_the_worker_listens_to_the_same_queues_as_the_makefile(self) -> None:
        """**Beat 排的每一個任務都必須有人消化**（test_platform_beat.py 的同一個洞）。

        本機的 `make worker` 與部署的 worker 若聽不同的佇列，那個差異只會在部署後
        出現，而症狀是「某一類任務在正式環境從來沒被執行過」。
        """
        makefile_queues = re.search(r"--queues\s+(\S+)", _MAKEFILE)
        assert makefile_queues, "WORKER_CMD 找不到 --queues"

        command = [str(part) for part in _services()["worker"]["command"]]
        compose_queues = command[command.index("--queues") + 1]

        assert set(compose_queues.split(",")) == set(makefile_queues.group(1).split(","))

    def test_the_worker_uses_the_threads_pool(self) -> None:
        """`--pool threads` 不是調校，是正確性：prefork 的 daemonic 行程不准開子行程，
        而 ETL 的抽取需要（forkserver 沙箱）。症狀是文件永遠停在 `parsing`，而
        API 側一切正常（Makefile 的 WORKER_CMD 註解記著這次教訓）。"""
        command = [str(part) for part in _services()["worker"]["command"]]

        assert command[command.index("--pool") + 1] == "threads"


class TestProbes:
    def test_the_healthcheck_uses_the_readiness_probe(self) -> None:
        """compose 的 healthcheck 決定 `depends_on: service_healthy`——那是就緒語意。

        打 `/healthz`（liveness）的話，DB 還沒起來的容器也會被判定健康，而 liveness
        的正確處置是重啟容器，不是等它就緒（11 §3.2）。
        """
        test = " ".join(str(part) for part in _services()["api"]["healthcheck"]["test"])

        assert "/readyz" in test, f"healthcheck 打的不是 /readyz：{test}"

    def test_the_probe_does_not_need_a_shell_tool(self) -> None:
        """image 是 slim base，沒有 curl 也沒有 wget。為了 healthcheck 裝一個，等於
        在部署映像裡多一個攻擊面——而症狀只是「healthcheck 永遠失敗」。"""
        test = " ".join(str(part) for part in _services()["api"]["healthcheck"]["test"])

        assert "curl" not in test and "wget" not in test


class TestExposure:
    def test_the_api_port_is_bound_to_loopback(self) -> None:
        """`"8000:8000"` 的簡寫等同 0.0.0.0（compose.yml 檔頭的同一條規則）。

        開發憑證是 `.env.example` 的 change-me-locally，接上公用 wifi 時同網段任何人
        都能拿全部租戶的資料，而本機完全不會有徵兆。
        """
        for mapping in _services()["api"]["ports"]:
            assert str(mapping).startswith("127.0.0.1:"), f"{mapping} 對整個網段開放"

    def test_only_the_api_publishes_a_port(self) -> None:
        """worker 與 beat 不對外開埠——它們沒有任何東西要服務，開了只是多一個面。"""
        services = _services()

        for role in ("worker", "beat"):
            assert "ports" not in services[role], f"{role} 不該對外開埠"

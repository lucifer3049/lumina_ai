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
    # 2A-2b：Beat 排程器。沒有它的話，分區維護／日結對帳／chunk 清理三個排程
    # 全部安靜地不跑——排程表是死的資料，得有一個行程照著它把任務丟進佇列。
    "beat": "BEAT_CMD",
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


class TestQueueCoverage:
    """每一條佇列都要有人消化——**這是與「服務漏接」同一種失敗**。

    1B-6 漏掉的是「worker 沒進 make start」，1C-3 可能漏掉的是「worker 起來了，但
    只吃 etl 佇列」。兩者的症狀一模一樣：訊息進得去、沒有人處理、API 側完全正常，
    只有文件永遠不會前進——前者停在 `uploaded`，後者停在 `chunked`。
    """

    def test_the_worker_consumes_every_routed_queue(self) -> None:
        from config.celery_app import celery_app

        routed = {route["queue"] for route in (celery_app.conf.task_routes or {}).values()}
        worker_command = _MAKEFILE.split("WORKER_CMD =", 1)[1].split("\n\n", 1)[0]
        consumed = set(worker_command.split("--queues", 1)[1].split()[0].split(","))

        assert routed <= consumed, f"沒有 worker 消化的佇列：{sorted(routed - consumed)}"


class TestWorkerPool:
    """worker 的 pool 型態——**1C-4 踩過的那個洞**。

    Celery 預設的 prefork pool 把工作行程建成 daemonic，而 daemonic 行程**不准有子
    行程**。抽取正是跑在子行程裡（08 §6 的隔離），所以預設值之下每一次上傳都會撞
    ``AssertionError: daemonic processes are not allowed to have children``——文件永遠
    停在 `parsing`，而 API 回 201、佇列深度正常、worker 也沒有掛掉。

    這個缺陷從 1B-6 就在，卻活過了整個 1B 與 1C-3。原因不是沒有測試，是**測試的形狀
    與部署的形狀不同**：smoke 的 worker fixture 當時寫 `--pool solo`（「少一層行程就
    少一種難以歸因的失敗」），剛好繞開了它。所以這裡守的是兩件事，而第二件比第一件
    重要——第一件擋的是這個 bug，第二件擋的是「下一個同類的 bug」。
    """

    @staticmethod
    def _worker_command() -> str:
        return _MAKEFILE.split("WORKER_CMD =", 1)[1].split("\n\n", 1)[0]

    @staticmethod
    def _pool_of(command: str) -> str:
        return command.split("--pool", 1)[1].split()[0].strip().strip('",')

    def test_the_worker_does_not_use_the_prefork_pool(self) -> None:
        """prefork 之下抽取一定失敗（見 class docstring）。"""
        command = self._worker_command()

        assert "--pool" in command, "沒有指定 pool——Celery 預設是 prefork，而那會讓抽取失敗"
        assert self._pool_of(command) != "prefork"

    def test_the_smoke_worker_uses_the_same_pool(self) -> None:
        """smoke 起的 worker 與 `make start` 起的必須是同一種 pool。

        **不同的話，smoke 驗的就不是會被部署的那個東西**——而差異落在哪一項是隨機的，
        這次剛好落在唯一會出事的那一項。
        """
        conftest = (_REPO_ROOT / "backend" / "tests" / "e2e" / "conftest.py").read_text(
            encoding="utf-8"
        )
        smoke_pool = conftest.split('"--pool",', 1)[1].split(",", 1)[0].strip().strip('"')

        assert smoke_pool == self._pool_of(self._worker_command()), (
            f"smoke 用 {smoke_pool}，make start 用 {self._pool_of(self._worker_command())}"
            "——smoke 驗的不是會被部署的形狀"
        )


class TestProviderVerification:
    """`make verify-provider` —— 唯一一條會打真 API 的路（1C-5）。

    **自動測試永遠不打真 API**（CLAUDE.md），所以 adapter 的驗收全是假的 HTTP 層：它驗
    得了「我們送出去的請求長什麼樣」，驗不了「那家真的收不收」。base_url 打錯一個字、
    認證標頭格式不對、Gemini 的相容端點吃不吃 `dimensions`——這幾類都要真的打一次才知道。

    折衷是把它做成**手動指令**：帶自己的金鑰跑，不進 CI、不進 `make test`。這裡守的是
    那條界線——它一旦被接進自動測試，CI 就會開始花錢，而且會因為別人的服務中斷而紅。
    """

    def test_the_target_exists(self) -> None:
        assert "verify-provider:" in _MAKEFILE

    def test_it_is_not_wired_into_the_automated_suites(self) -> None:
        """`make test` / `make lint` / CI 都不得依賴它。"""
        import re

        for target in ("test", "lint", "smoke"):
            match = re.search(rf"^{target}:.*$", _MAKEFILE, re.MULTILINE)
            assert match is not None, f"找不到 {target} 目標"
            assert "verify-provider" not in match.group(0)

    def test_ci_does_not_call_it(self) -> None:
        workflow = (_REPO_ROOT / ".github" / "workflows").glob("*.yml")

        for path in workflow:
            assert "verify-provider" not in path.read_text(encoding="utf-8"), (
                f"{path.name} 會打真 API——CI 會開始花錢，且會因為別人的服務中斷而紅"
            )

    def test_smoke_subprocesses_are_forced_onto_the_mock_provider(self) -> None:
        """smoke 起的 API 與 worker 都不得打真 API（1D-5 發現，2026-08-17）。

        **`config/settings/test.py` 的強制值救不了 smoke**：那份只對 in-process 的測試
        套件有效，而 smoke 的兩個子行程跑在 `config.settings.dev` 之下——那是正式的
        設定路徑，會照實讀 repo 根的 `.env`。1C-5 把真金鑰寫進 `.env` 之後，smoke 就
        一直在打真的 Gemini，而沒有任何測試會因此變紅。

        最難查的代價不是花錢，是**兩個子行程可能落在不同的模型上**：worker 用 A 模型
        寫向量、API 用 B 模型查，`UNIQUE(chunk_id, model, embedding_version)` 對不上，
        檢索永遠回零筆。文件照樣 `ready`、API 全部 200，畫面上只是「答不出東西」。
        這條測試守的就是那個前提。
        """
        conftest = (_REPO_ROOT / "backend" / "tests" / "e2e" / "conftest.py").read_text(
            encoding="utf-8"
        )
        forced = conftest.split("_MOCK_AI_ENV", 1)[1].split("}", 1)[0]

        for key in ("AI_EMBEDDING_PROVIDER", "AI_CHAT_PROVIDER"):
            assert f'"{key}": "mock"' in forced, f"smoke 沒有把 {key} 釘成 mock"
        for key in ("AI_EMBEDDING_API_KEY", "AI_CHAT_API_KEY"):
            assert f'"{key}": ""' in forced, f"smoke 沒有清掉 {key}——金鑰還在就花得了錢"
        assert "**_MOCK_AI_ENV" in conftest, "強制值定義了卻沒有套進子行程的環境"

    def test_smoke_has_its_own_celery_queue(self) -> None:
        """smoke 與 `make start` 不得共用同一個 Redis 邏輯 DB（1D-5 發現，2026-08-17）。

        **Celery 的工作籃就是 Redis 的一個 DB。** 共用的話，`make start` 起的 worker 會
        撿走 smoke 的任務——而它讀的是 repo 根的 `.env`（真 provider），於是寫入端與
        查詢端落在不同的模型上，檢索永遠回零筆。文件照樣 `ready`、API 全部 200，
        smoke 只說「沒有引用」，而 dev 環境可能已經開了一整天沒人記得。

        分工：0 給 dev 與 `make start`、1~14 給 xdist worker、15 給 smoke。
        """
        conftest = (_REPO_ROOT / "backend" / "tests" / "e2e" / "conftest.py").read_text(
            encoding="utf-8"
        )
        root_conftest = (_REPO_ROOT / "backend" / "tests" / "conftest.py").read_text(
            encoding="utf-8"
        )
        smoke_db = conftest.split("_SMOKE_REDIS_DB = ", 1)[1].split("\n", 1)[0].strip().strip('"')

        assert smoke_db != "0", "smoke 與 make start 共用 DB 0——會互相搶任務"
        assert '"REDIS_DB": _SMOKE_REDIS_DB' in conftest, "定義了卻沒套進子行程的環境"
        assert f"+ 1 > {int(smoke_db) - 1}" in root_conftest, (
            f"xdist 的上限沒有讓開 DB {smoke_db}——平行測試會與 smoke 撞在一起"
        )


class TestObservability:
    def test_status_lists_every_service(self) -> None:
        listed = _DEV_SH.split("for name in ", 1)[1].split(";", 1)[0].split()

        assert set(listed) == set(SERVICES)

    def test_logs_include_every_service(self) -> None:
        tail_line = next(line for line in _DEV_SH.splitlines() if "tail -f" in line)

        for service in SERVICES:
            assert f"log_file {service}" in tail_line


class TestTeiStaysOptional:
    """TEI 容器（2B-4）：**開發機上的一個選配服務**，不是這個 repo 的相依。

    13 §4 的定案寫得很直白——沒有 GPU 的機器（CI、他機）不得因為 `make up` 起不了 TEI
    而卡住。守的是三件事：它不預設啟動、自動化套件不碰它、CI 不知道它存在。
    """

    def test_it_has_its_own_target(self) -> None:
        """`make tei-up`：要跑真 rerank 的人有一個明確的動作，而不是「改 compose 再
        自己下 docker compose」——後者的參數（profile、GPU、port）每個人都寫得不一樣。"""
        assert "tei-up:" in _MAKEFILE

    @pytest.mark.parametrize("target", ["up", "test", "lint", "smoke"])
    def test_the_everyday_targets_do_not_need_a_gpu(self, target: str) -> None:
        import re

        match = re.search(rf"^{target}:.*(?:\n\t.*)*", _MAKEFILE, re.MULTILINE)

        assert match is not None, f"找不到 {target} 目標"
        assert "tei" not in match.group(0), f"{target} 依賴了 TEI——沒有 GPU 的機器會卡住"

    def test_ci_does_not_start_it(self) -> None:
        for path in (_REPO_ROOT / ".github" / "workflows").glob("*.yml"):
            assert "tei-up" not in path.read_text(encoding="utf-8"), (
                f"{path.name} 想在 CI 上起 TEI——CI runner 沒有 GPU"
            )

    def test_the_manual_verification_covers_rerank(self) -> None:
        """`make verify-provider CAPABILITY=rerank`：adapter 的驗收全走 MockTransport，
        驗得了「我們送出去的長什麼樣」，驗不了「TEI 真的收不收」。base_url 差一個字、
        欄位名拼錯、分數尺度不是 0~1——這幾類都要真的打一次才知道。"""
        script = (_REPO_ROOT / "backend" / "scripts" / "verify_provider.py").read_text(
            encoding="utf-8"
        )

        assert '"rerank"' in script, "verify_provider 還不認得 rerank capability"

    def test_smoke_subprocesses_are_forced_onto_the_mock_reranker(self) -> None:
        """smoke 的兩個子行程讀的是 repo 根的 `.env`（見上一個 class 的說明），而 2B-4
        之後那份檔案會指向真的 TEI 或帶著 Jina 的金鑰。

        釘死它的理由與 embedding／chat 那兩條一樣，只是後果更難查：rerank 失敗會**降級**
        而不是報錯，所以 TEI 沒開的 smoke 仍然全綠——綠的是「降級鏈有效」，而不是
        「rerank 正常」，兩者在 smoke 的輸出裡長得一模一樣。
        """
        conftest = (_REPO_ROOT / "backend" / "tests" / "e2e" / "conftest.py").read_text(
            encoding="utf-8"
        )
        forced = conftest.split("_MOCK_AI_ENV", 1)[1].split("}", 1)[0]

        assert '"AI_RERANK_PROVIDER": "mock"' in forced, "smoke 沒有把 rerank 釘成 mock"
        assert '"AI_RERANK_API_KEY": ""' in forced, "smoke 沒有清掉 rerank 金鑰"

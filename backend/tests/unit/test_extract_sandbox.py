"""驗收：抽取的子行程隔離與毒檔防護（08 §6、13 §3 工作包 1B-4）。

**為什麼抽取一定要跑在子行程裡**：解析是唯一一段會吃使用者提供的**二進位結構**的
程式碼。畸形 PDF、zip bomb、深度巢狀的 XML 都可能讓函式庫吃光記憶體或無限迴圈，
而那不是可以用 try/except 接住的東西——OOM 殺掉的是整個行程，無限迴圈根本不會回來。
worker 掛掉的後果是那台機器上**所有租戶**的 ETL 一起停擺，而不只是這一份毒檔失敗。

因此本檔驗的是四件事，每一件都對應一種「不隔離就會發生」的災難：

1. **逾時會被砍**，而且不留孤兒行程。
2. **記憶體上限會生效**（上限套在子行程，觸發時死的是它）。
3. **子行程崩潰（segfault 等）不會拖垮父行程**，會轉成可處理的例外。
4. 正常情況下**結果拿得回來**——隔離不能把功能弄丟。

「真的有換行程」由 TestSuccessPath 用回傳路徑釘一次；孤兒檢查對 **OS** 驗證
（子行程回報真實 pid，逾時後斷言該 pid 已不存在），而不是對函式庫驗證——
`multiprocessing.active_children()` 只看得到經 multiprocessing 建立的子行程，
實作換成 subprocess / os.fork 時那種斷言會對兩個空集合恆真。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from core.exceptions import ExtractionFailedError
from etl.extract.sandbox import run_isolated


def _sleep_forever(_: bytes) -> object:
    while True:
        time.sleep(0.1)


def _report_pid_then_sleep(payload: bytes) -> object:
    # payload 是 pid 檔路徑：把真實 pid 交回父行程，讓測試能對 OS 驗證行程已消失。
    Path(payload.decode()).write_text(str(os.getpid()), encoding="utf-8")
    while True:
        time.sleep(0.1)


def _eat_memory(_: bytes) -> object:
    chunks = []
    while True:
        # 每次 16MB：太小的話光是迴圈次數就先撞到逾時，測到的是錯的東西。
        chunks.append(bytearray(16 * 1024 * 1024))


def _crash(_: bytes) -> object:
    # 直接讓子行程死掉（不是 raise）——模擬 C 擴充的 segfault，那正是 try/except
    # 接不住、而隔離唯一擋得住的情況。
    os._exit(139)


def _echo(payload: bytes) -> object:
    return {"echoed": payload.decode()}


def _raise_value_error(_: bytes) -> object:
    raise ValueError("解析失敗")


class TestSuccessPath:
    def test_result_comes_back_from_the_child(self) -> None:
        assert run_isolated(_echo, b"hello", timeout_seconds=10) == {"echoed": "hello"}

    def test_parent_pid_is_unchanged(self) -> None:
        """順帶釘住「真的有換行程」：結果是從別的行程回來的。"""
        before = os.getpid()

        run_isolated(_echo, b"x", timeout_seconds=10)

        assert os.getpid() == before


class TestPoisonedDocuments:
    def test_timeout_kills_the_child(self) -> None:
        """無限迴圈 → 逾時被砍，且失敗原因是**結構化**的。

        08 §6 的「超時 kill」。沒有它的話，一份畸形 PDF 會讓一個 worker 永遠卡住，
        而佇列裡其他租戶的工作只會愈積愈多——監控上看到的是「ETL 變慢」，不是
        「有一份毒檔」。

        斷言 ``details["cause"]`` 而不是訊息字串：重試判定（逾時不重試）要靠它
        分類，而訊息措辭是給人看的、允許改（同 test_original_exception_type_...
        的理由）。
        """
        with pytest.raises(ExtractionFailedError) as caught:
            run_isolated(_sleep_forever, b"", timeout_seconds=1)

        assert caught.value.details.get("cause") == "timeout"

    def test_timeout_actually_stops_the_child(self, tmp_path: Path) -> None:
        """砍完之後不得留下孤兒行程——對 OS 驗證，不對函式庫驗證。

        只是「不等它」的實作（例如把 join 加上 timeout 就回傳）在這條測試下會露餡：
        那個子行程會繼續跑到天荒地老，而 worker 的 CPU 從此少一顆。
        """
        pid_path = tmp_path / "child.pid"

        with pytest.raises(ExtractionFailedError):
            run_isolated(_report_pid_then_sleep, str(pid_path).encode(), timeout_seconds=1)

        assert pid_path.exists(), "子行程沒跑起來就逾時——payload 沒送達或 timeout 過短"
        child_pid = int(pid_path.read_text(encoding="utf-8"))
        # 給 kill 一點時間落地；行程消失（或成為已回收的殭屍）即通過。
        for _ in range(20):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"逾時後子行程 {child_pid} 仍存活")

    def test_memory_limit_is_enforced(self) -> None:
        """吃光記憶體 → 子行程死，父行程活著。

        這是 zip bomb 與「解壓後 40GB」那類檔案唯一擋得住的地方。上限套在**子行程**
        （``RLIMIT_AS``），所以觸發時死的是它。父行程若沒有隔離，這裡會直接被 OOM
        killer 收掉——而那在 CI 上表現為「測試整個消失」而不是失敗。
        """
        with pytest.raises(ExtractionFailedError):
            run_isolated(_eat_memory, b"", timeout_seconds=30, memory_limit_bytes=256 * 1024 * 1024)

    def test_child_crash_becomes_an_exception(self) -> None:
        """子行程 segfault → `ExtractionFailedError`，不是父行程跟著死。"""
        with pytest.raises(ExtractionFailedError):
            run_isolated(_crash, b"", timeout_seconds=10)


class TestErrorPropagation:
    def test_exception_inside_the_child_is_reported(self) -> None:
        """子行程裡的例外要傳回父行程，且**訊息看得出原因**。

        只回「子行程失敗」的話，08 §6 的 DLQ 內容（stage、原因、可否重跑）就只剩
        stage——維運看到一堆一樣的失敗紀錄，分不出是毒檔還是我們的 bug。
        """
        with pytest.raises(ExtractionFailedError) as caught:
            run_isolated(_raise_value_error, b"", timeout_seconds=10)

        assert "解析失敗" in str(caught.value)

    def test_original_exception_type_is_recorded_in_details(self) -> None:
        """原始例外型別留在 ``details`` 裡供分類。

        重試判定要靠它（08 §6：OOM / 毒檔不重試，暫時性錯誤才重試）。放在 details
        而不是訊息裡，是因為那是給程式看的，不是給人看的。
        """
        with pytest.raises(ExtractionFailedError) as caught:
            run_isolated(_raise_value_error, b"", timeout_seconds=10)

        assert caught.value.details.get("cause") == "ValueError"

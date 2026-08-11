"""抽取的行程隔離 —— 毒檔防護（08 §6）。

**解析是唯一一段會吃使用者提供的二進位結構的程式碼。** 畸形 PDF、zip bomb、深度
巢狀的 XML 都可能讓解析函式庫吃光記憶體或無限迴圈，而那兩種都不是 ``try/except``
接得住的：OOM 殺掉的是整個行程，無限迴圈根本不會回來。worker 掛掉的後果是那台機器
上**所有租戶**的 ETL 一起停擺，而不只是這一份毒檔失敗。

因此抽取跑在子行程裡，父行程只負責三件事：等、砍、把失敗轉成 `ExtractionFailedError`。

**POSIX only**（``fork`` + ``resource``）。部署與開發環境都是 Linux 容器 / WSL2
（見 02 §2 的執行環境），因此不做跨平台後備——寫一條在 Windows 上「看起來能跑但沒有
記憶體上限」的路徑，只會讓防護在最需要的地方安靜消失。

**用 ``forkserver`` 而不是 ``fork``。** 直接 fork 有兩個實質缺陷，兩個都會在
worker 上線後才發作：

1. **鎖的繼承**。fork 只保留呼叫它的那個執行緒，記憶體卻整份照抄——父行程在 fork
   當下被背景執行緒持有的鎖（psycopg pool、logging、structlog），在子行程裡是「已
   上鎖而持有者不存在」，永遠不會釋放。症狀是某些文件的抽取毫無理由卡到逾時、重跑
   又好了，而 log 裡沒有任何線索。CPython 3.12 起對多執行緒行程呼叫 fork 直接發
   ``DeprecationWarning``，講的就是這件事。
2. **記憶體上限失真**。``RLIMIT_AS`` 管的是位址空間，而 fork 出來的子行程繼承整個
   Django + 解析函式庫的映射。上限因此必須訂得比父行程的 VSZ 高，「這份文件可以用
   多少記憶體」根本無從表達。

forkserver 兩個都解：實際 fork 的動作發生在一個乾淨、單執行緒、由 multiprocessing
維護的中介行程裡。代價是被執行的函式必須可 pickle（模組層級函式即可）。

刻意**不設 ``set_forkserver_preload``**：預載會把解析函式庫的映射灌進中介行程，於是
上面第 2 點又回來了。每份文件多一次 import（數百毫秒）對分鐘級 SLO 的 ETL 無感，
換到的是「記憶體上限量的是這份文件真正用掉的量」。
"""

from __future__ import annotations

import multiprocessing
import os
import resource
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any, cast

from core.exceptions import ExtractionFailedError

# 子行程回報的訊息形狀：("ready",) → ("ok", result) 或 ("error", 例外類別名, 訊息)。
_READY = "ready"
_OK = "ok"
_ERROR = "error"

# 子行程從被建立到**開始執行 func** 的預算，與抽取本身的 ``timeout_seconds`` 分開計。
# forkserver 的中介行程是 lazy 啟動，而子行程還要 import 解析函式庫——那段時間可以
# 是數百毫秒到數秒，把它算進抽取逾時的話，「這份 PDF 解析太久」與「機器正忙」會變成
# 同一個 cause，而兩者的重試策略相反（前者不該重試，後者該）。
_BOOTSTRAP_TIMEOUT_S = 60.0

# 砍掉子行程後等它被回收的上限。SIGKILL 之後不該需要這麼久；等不到就別再卡著
# 呼叫端——父行程本來就是要繼續處理下一份文件的。
_REAP_TIMEOUT_S = 5.0

# 模組層級取一次：context 本身無狀態，但中介行程是 lazy 啟動且**每個 context 一份**。
# 每次呼叫都建新 context 的話，每份文件都會多啟一個中介行程。
_CONTEXT = multiprocessing.get_context("forkserver")


def run_isolated[T](
    func: Callable[[bytes], T],
    payload: bytes,
    *,
    timeout_seconds: float,
    memory_limit_bytes: int | None = None,
) -> T:
    """在子行程中執行 ``func(payload)``，逾時或崩潰一律轉成 `ExtractionFailedError`。

    ``func`` 必須是**模組層級**函式（forkserver 以 pickle 傳遞它）。lambda 與區域
    函式會在送出時就失敗，而不是跑到一半才出事——這是刻意的取捨，見模組 docstring。

    ``memory_limit_bytes`` 套在子行程的 ``RLIMIT_AS``（位址空間）。中介行程是乾淨的，
    因此這個值接近「這份文件可以用多少」；仍需留出 Python 直譯器與解析函式庫 import
    後的基線（實測數百 MB 起跳），訂太小會在跑到 ``func`` 之前就失敗。

    軟上限設為 ``memory_limit_bytes``、硬上限維持不變：觸發 ``MemoryError`` 之後，
    子行程要能把軟上限放回去才有記憶體可用來組錯誤訊息並回報。兩個都調死的話，
    父行程只會看到一個沒有原因的崩潰。
    """
    receiver, sink = _CONTEXT.Pipe(duplex=False)
    process = _CONTEXT.Process(
        target=_child_main,
        args=(func, payload, sink, memory_limit_bytes),
        daemon=True,
    )
    process.start()
    # 父行程要關掉自己那一端的寫入口，否則子行程死掉時 pipe 不會 EOF——
    # 「崩潰」會表現成「逾時」，而那兩者的重試策略不同。
    sink.close()

    try:
        # 先等「我開始跑了」。這一段等的是行程啟動，不是解析。
        if not receiver.poll(_BOOTSTRAP_TIMEOUT_S):
            raise ExtractionFailedError(
                f"抽取子行程啟動逾時（{_BOOTSTRAP_TIMEOUT_S:g}s）",
                cause="child_bootstrap_timeout",
            )
        try:
            handshake = cast(tuple[Any, ...], receiver.recv())
        except (EOFError, OSError) as exc:
            raise ExtractionFailedError(
                "抽取的子行程啟動失敗",
                cause="child_crashed",
                details={"exitcode": process.exitcode},
            ) from exc
        if handshake[0] != _READY:
            # 子行程在送出 ready 之前就失敗（例如記憶體上限低到連 import 都做不完）。
            raise ExtractionFailedError(
                f"抽取失敗：{handshake[2]}" if len(handshake) > 2 else "抽取失敗",
                cause=str(handshake[1]) if len(handshake) > 1 else "child_failed",
            )

        if not receiver.poll(timeout_seconds):
            raise ExtractionFailedError(
                f"抽取逾時（{timeout_seconds:g}s）",
                cause="timeout",
                details={"timeout_seconds": timeout_seconds},
            )
        try:
            message = cast(tuple[Any, ...], receiver.recv())
        except (EOFError, OSError) as exc:
            # 子行程沒送出任何東西就消失了：segfault、被 OOM killer 收掉、
            # 或 ``os._exit``。exitcode 在 `_terminate` 之後才穩定，這裡先記狀態。
            raise ExtractionFailedError(
                "抽取的子行程異常結束",
                cause="child_crashed",
                details={"exitcode": process.exitcode},
            ) from exc
    finally:
        _terminate(process)
        receiver.close()

    if message[0] == _ERROR:
        _, cause, detail = message
        raise ExtractionFailedError(f"抽取失敗：{detail}", cause=str(cause))

    return cast(T, message[1])


def _terminate(process: multiprocessing.process.BaseProcess) -> None:
    """確保子行程真的消失，且被回收。

    ``join`` 不可省：沒有回收的子行程會留成殭屍，pid 依然存在（``kill(pid, 0)``
    照樣成功），而長跑的 worker 會慢慢把 pid 表填滿。
    """
    if process.is_alive():
        process.kill()
    process.join(_REAP_TIMEOUT_S)
    if not process.is_alive():
        # 還活著就不能 close（會 ValueError）；SIGKILL 之後等不到回收屬異常狀況，
        # 留給行程表而不是在這裡再丟一個蓋掉真正原因的例外。
        process.close()


def _child_main(
    func: Callable[[bytes], Any],
    payload: bytes,
    sink: Connection,
    memory_limit_bytes: int | None,
) -> None:
    """子行程的進入點。**永遠以 ``os._exit`` 結束。**

    正常 return 會讓 multiprocessing 跑完 atexit 與 pytest 的收尾程序——在一個
    fork 出來的行程裡，那會重複執行父行程的清理（關連線、寫報告），症狀是父行程
    的資源被子行程關掉。
    """
    restore = _apply_memory_limit(memory_limit_bytes)
    try:
        # 握手放在記憶體上限**之後**：上限本身若低到讓子行程活不下來，那要表現成
        # 啟動失敗，而不是一個永遠等不到的 ready。
        _send(sink, (_READY,))
        result = func(payload)
    # 子行程裡的**任何**失敗都要送回父行程——包含 MemoryError 這種
    # 通常不該攔的 BaseException：不攔的話父行程只看得到一個沒有原因的崩潰。
    except BaseException as exc:
        # 先把記憶體上限放回去：MemoryError 之下連組一個字串都可能失敗，
        # 而回報不出原因的失敗在 DLQ 裡與崩潰無法區分。
        restore()
        _send(sink, (_ERROR, type(exc).__name__, str(exc)))
    else:
        _send(sink, (_OK, result))
    finally:
        sink.close()
        os._exit(0)


def _apply_memory_limit(memory_limit_bytes: int | None) -> Callable[[], None]:
    """套用 ``RLIMIT_AS`` 軟上限，回傳「放回原值」的函式。"""
    if memory_limit_bytes is None:
        return lambda: None

    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, hard))

    def restore() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (soft, hard))

    return restore


def _send(sink: Connection, message: tuple[Any, ...]) -> None:
    """回報結果。送不出去（pickle 失敗、父行程已消失）時降級成型別名。

    降級而不是沉默：父行程對「沒有訊息」的解讀是崩潰，而「結果算出來了但送不回去」
    是我們自己的 bug——把它記成一個看得出來的 cause，才有人會去修。
    """
    try:
        sink.send(message)
    except (BrokenPipeError, OSError):
        return
    except Exception as exc:  # pickle 失敗屬此
        try:
            sink.send((_ERROR, "unserializable_result", f"{type(exc).__name__}: {exc}"))
        except (BrokenPipeError, OSError):
            return

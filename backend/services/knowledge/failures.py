"""失敗紀錄的統一形狀（08 §6 的 DLQ 內容）。

ETL 與 embedding 兩條 pipeline 都會把失敗寫進 `document.error`，而那份 dict 會經
`DocumentOut.error` **回到租戶手上**。這裡只有一個函式，但它必須只有一份：1B 結案
review 抓到的正是「第三方例外的字串外洩內部細節」（botocore 夾 endpoint 與 bucket
名、psycopg 夾表名與 SQL 片段），而 provider SDK 的錯誤字串還會夾 API key 前綴。

各寫一份的話，修好的永遠只有被審到的那一份，而另一份沒有任何症狀——它洩漏的對象
不會來回報。
"""

from __future__ import annotations

from typing import Any

from core.exceptions import DomainError

# 非自家例外對外顯示的固定訊息。
INTERNAL_FAILURE_MESSAGE = "處理失敗，請稍後重試或聯絡管理員"


def error_payload(exc: Exception) -> dict[str, Any]:
    """結構化的失敗紀錄。

    ``cause`` 給程式看（重試判定、統計），``message`` 給人看。只寫訊息的話，維運
    看到的是一堆措辭不同的字串，分不出毒檔與我們的 bug。

    **只有自家例外的訊息會被寫進去**（鐵則 9）。型別名（``cause``）一律留著——分類
    與統計要用它，而它不洩漏內容。完整訊息只進 log（worker 那一層帶 ``exc_info``）。
    """
    if isinstance(exc, DomainError):
        return {"cause": str(exc.details.get("cause") or type(exc).__name__), "message": str(exc)}
    return {"cause": type(exc).__name__, "message": INTERNAL_FAILURE_MESSAGE}

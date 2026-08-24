"""請求層的追蹤 context、存取日誌與回應標頭（12 §1.1）。

自 `api/main.py` 搬過來（2A-4 拆檔，內容未變）：那個檔的 docstring 早就寫著
「拆檔留給第一個真的需要第二條 middleware 的工作包」。

**它必須是最外層**：`clear_current_tenant_id()` 在這裡的 finally，任何需要租戶
contextvar 的 middleware（稽核）都得跑在它裡面。
"""

from __future__ import annotations

import time
import uuid

from fastapi import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from config.logging import bind_request_context, clear_request_context, get_logger
from core.request_cache import request_cache
from core.tenant import clear_current_tenant_id

logger = get_logger(__name__)

ACCESS_EVENT = "http_request"
REQUEST_ID_HEADER = "X-Request-Id"

__all__ = ["ACCESS_EVENT", "REQUEST_ID_HEADER", "RequestContextMiddleware", "request_id_of"]


def request_id_of(request: Request) -> str:
    """取本次請求的追蹤 id；middleware 沒跑到時就地補一個。

    刻意**不採信** client 送來的 ``X-Request-Id``：那是未驗證輸入，直接寫進
    log 等於讓外部污染我們的追蹤欄位。id 一律由服務端產生。
    """
    request_id = getattr(request.state, "request_id", None)
    if not isinstance(request_id, str):
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
    return request_id


class RequestContextMiddleware:
    """請求層的追蹤 context、存取日誌、回應標頭，以及請求結束時的 context 收尾。

    **三件事合成一條 middleware 是刻意的**，前一版是三條互相依賴的
    ``BaseHTTPMiddleware``，代價是三個問題：短路的回應不會被記錄、順序約束只寫在
    註解裡沒有測試釘住、每條 middleware 每個請求都要建一組 anyio task group 加
    兩個 memory object stream（落在壓測的熱路徑上）。

    **為什麼是純 ASGI 而不是 ``@app.middleware("http")``**（1A-3 改）：
    ``BaseHTTPMiddleware`` 的 ``call_next`` 把下游丟到**另一個 task** 執行，而
    contextvars 的修改不會從子 task 回流到父 task。租戶來自 route 層的認證
    ``Depends``，那是下游，於是這裡讀到的永遠是空的，**每一筆存取日誌的
    tenant_id 都會靜靜消失**（13 §3.2）。純 ASGI 的 ``await self.app(...)`` 就在
    同一個 task 裡，下游設定的 contextvar 在它回來之後讀得到。

    ``path`` 刻意不含 query string：``?token=...`` 這類值會整串落地成明文
    （鐵則 9）。需要查參數時走 audit log，那裡有欄位級的遮罩政策。
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive)
        request_id = request_id_of(request)
        started = time.perf_counter()

        bind_request_context(request_id=request_id)

        status_seen = 500

        async def send_with_request_id(message: Message) -> None:
            """在回應開頭注入 X-Request-Id，並記下狀態碼供存取日誌使用。"""
            nonlocal status_seen
            if message["type"] == "http.response.start":
                status_seen = int(message["status"])
                headers = list(message.get("headers", []))
                # 只在下游沒設過時補：problem_response 自己會帶，重複附加會讓
                # 標頭變成 "id, id"（HTTP 允許同名標頭合併），client 拿去查 log
                # 就會查不到——而回應看起來完全正常。
                key = REQUEST_ID_HEADER.lower().encode()
                if not any(name.lower() == key for name, _ in headers):
                    headers.append((key, request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        def emit(status: int) -> None:
            # tenant_id 不在這裡取值：config/logging.py 的 tenant_processor 會在
            # **寫出當下**讀 contextvar，因此 route 層 Depends 設定的租戶也涵蓋得到。
            logger.info(
                ACCESS_EVENT,
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )

        try:
            # 請求級 memo 的邊界（core/request_cache.py，二次架構審計 F-03）。
            # **開在最外層**：它要蓋住 route 層的 Depends（認證）與整條 service
            # 呼叫鏈，而那是這條 middleware 唯一涵蓋得到全程的地方。
            with request_cache():
                await self._app(scope, receive, send_with_request_id)
        except Exception:
            # 例外會往上冒到 ServerErrorMiddleware 才變成 500 回應，那已在本層之外。
            # 不在這裡補一筆就會出現「有錯誤 log、卻沒有對應的存取記錄」的缺口。
            emit(500)
            raise
        else:
            emit(status_seen)
        finally:
            # 兩個 context 都要收：租戶由 route 層的認證 Depends 設定，而那裡沒有
            # 涵蓋整個請求的 finally——收尾只能在這一層。省略時前一個請求的租戶會
            # 留在 context 裡給下一個請求用（見 core/tenant.py 的說明），症狀是
            # log 標到別的租戶、查詢在 RLS 之下讀到那個租戶的資料。
            clear_current_tenant_id()
            clear_request_context()

"""請求 body 的大小上限 —— 在解析之前擋下（09 §3.1、10 §99）。

`UploadFile` 的內容在 endpoint 拿到它**之前**就已經被 Starlette 的 multipart 解析器
讀完並 spool 到磁碟了，所以 endpoint 裡再怎麼小心也只擋得住「載回記憶體」那一段。
一個 2GB 的請求仍然會被完整收下、寫進暫存檔——幾個併發就是一次磁碟或記憶體事故，
而 uvicorn 預設沒有 body 大小限制。

這一層看的是 ``Content-Length``：**一個標頭就決定收不收**，body 一個位元組都不讀。

**擋不住的兩種情況，各自有人接**：

- 標頭缺席（``Transfer-Encoding: chunked``）或說謊報小 → 由 endpoint 的分塊讀取
  （`api/v1/knowledge.py` 的 `_read_within_limit`）擋住載回記憶體那一段。
- 惡意的大流量本身 → 那是部署層的事（nginx／ingress 的 ``client_max_body_size``），
  應用程式收到請求時頻寬已經花掉了。**這一道要記在部署清單上**，程式碼裡補不了。

**為什麼是純 ASGI 而不是 `BaseHTTPMiddleware`**：理由同 `request_context.py`——後者
把下游丟到另一個 task，而且每個請求都要建一組 task group 加兩個 stream，落在所有
請求的熱路徑上。這裡只讀一個標頭。
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from core.exceptions import ErrorCode

__all__ = ["MAX_REQUEST_BYTES", "BodySizeLimitMiddleware"]

# 上限比單檔上限（32MB）寬一點：multipart 的邊界、標頭與檔名也算在 body 裡，卡在
# 剛好等於檔案上限的話，一個剛好 32MB 的合法上傳會被這一層誤殺，而錯誤訊息會說
# 「超過 32MB」——使用者看著自己 32MB 的檔案完全無法理解。
#
# 真正判定「這份檔案收不收」的仍然是 `services/knowledge/uploads.ensure_within_limit`
# （單一事實來源）；這裡只負責「明顯過大的請求連解析都不要開始」。
_MULTIPART_OVERHEAD_BYTES = 1024 * 1024


def _limit() -> int:
    from services.knowledge.uploads import MAX_UPLOAD_BYTES

    return MAX_UPLOAD_BYTES + _MULTIPART_OVERHEAD_BYTES


MAX_REQUEST_BYTES = _limit()


class BodySizeLimitMiddleware:
    """``Content-Length`` 超過上限 → 直接 413，不進下游。"""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _too_large(scope):
            await self._app(scope, receive, send)
            return

        # **自己組回應而不是 raise**：例外處理器掛在 router 外圍的
        # `ExceptionMiddleware` 上，而 middleware 跑在它**外面**——在這裡 raise 只會
        # 得到 ServerErrorMiddleware 的純文字 500，那比沒有這道防線更難查。
        from starlette.requests import Request

        from api.main import problem_response  # 函式內 import：api.main 會 import 本模組
        from api.middleware.request_context import request_id_of

        response = problem_response(
            status=413,
            code=str(ErrorCode.UPLOAD_TOO_LARGE),
            detail=f"請求內容超過上限（{MAX_REQUEST_BYTES} 位元組）",
            request_id=request_id_of(Request(scope, receive)),
        )
        await response(scope, receive, send)


def _too_large(scope: Scope) -> bool:
    """只看 ``Content-Length``。解不開的值當成沒給——那是 client 的格式問題，
    由下游的解析器給出它自己的錯誤，不該在這裡變成「太大」。"""
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-length":
            try:
                return int(value) > MAX_REQUEST_BYTES
            except ValueError:
                return False
    return False

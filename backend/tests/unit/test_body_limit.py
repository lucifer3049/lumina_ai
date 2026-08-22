"""驗收：請求 body 的大小上限（`api/middleware/body_limit.py`、09 §3.1）。

`UploadFile` 的內容在 endpoint 拿到它**之前**就已經被 multipart 解析器讀完並 spool 到
磁碟了——所以 endpoint 裡再小心也只擋得住「載回記憶體」那一段。一個 2GB 的請求仍然
會被完整收下，而 uvicorn 預設沒有 body 大小限制。

這一層的價值在於**它什麼都不讀**：看一個標頭就決定收不收。因此本檔最重要的斷言不是
「回了 413」，而是**下游一次都沒有被呼叫**。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from api.middleware.body_limit import MAX_REQUEST_BYTES, BodySizeLimitMiddleware


class _Downstream:
    """記錄自己有沒有被呼叫過的假 app。"""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _scope(content_length: str | None, *, kind: str = "http") -> dict[str, Any]:
    headers = [(b"host", b"testserver")]
    if content_length is not None:
        headers.append((b"content-length", content_length.encode()))
    return {"type": kind, "method": "POST", "path": "/api/v1/x", "headers": headers}


async def _run(scope: dict[str, Any]) -> tuple[_Downstream, list[dict[str, Any]]]:
    downstream = _Downstream()
    sent: list[dict[str, Any]] = []

    async def receive() -> Any:  # pragma: no cover —— 被擋下時不該被呼叫
        raise AssertionError("擋下的請求不該讀 body")

    async def send(message: Any) -> None:
        sent.append(message)

    await BodySizeLimitMiddleware(downstream)(scope, receive, send)
    return downstream, sent


def _body(sent: list[dict[str, Any]]) -> dict[str, Any]:
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return dict(json.loads(payload))


class TestOversized:
    async def test_it_never_reaches_the_application(self) -> None:
        """**這條才是重點。** 回 413 但仍然把 2GB 收下來的話，這道防線等於不存在。"""
        downstream, _ = await _run(_scope(str(MAX_REQUEST_BYTES + 1)))

        assert downstream.calls == 0

    async def test_it_answers_413_with_the_contract_shape(self) -> None:
        """錯誤格式與其他所有錯誤相同（09 §1.3 的 Problem Details）——middleware 跑在
        exception handler **外面**，所以這裡是自己組的，容易與契約漂掉。"""
        _, sent = await _run(_scope(str(MAX_REQUEST_BYTES + 1)))

        assert sent[0]["status"] == 413
        assert _body(sent)["code"] == "UPLOAD_TOO_LARGE"

    async def test_the_response_carries_a_request_id(self) -> None:
        """413 若在日誌上追不回來，「使用者說傳不上去」就查不出是被哪一道擋的。"""
        _, sent = await _run(_scope(str(MAX_REQUEST_BYTES + 1)))

        assert _body(sent)["request_id"]


class TestPassThrough:
    @pytest.mark.parametrize(
        ("content_length", "why"),
        [
            (str(MAX_REQUEST_BYTES), "剛好等於上限要放行——邊界寫錯的話合法上傳會被誤殺"),
            ("0", "沒有 body 的請求（GET/DELETE）"),
            (None, "沒有 Content-Length（chunked）——由 endpoint 的分塊讀取接手"),
            ("abc", "解不開的值是 client 的格式問題，交給下游的解析器報它自己的錯"),
        ],
    )
    async def test_it_goes_through(self, content_length: str | None, why: str) -> None:
        downstream, sent = await _run(_scope(content_length))

        assert downstream.calls == 1, why
        assert sent[0]["status"] == 200

    async def test_non_http_scopes_are_untouched(self) -> None:
        """lifespan／websocket 沒有 body 的概念，攔了只會讓啟動流程掛掉。"""
        downstream, _ = await _run(_scope(None, kind="lifespan"))

        assert downstream.calls == 1


class TestLimitItself:
    def test_it_is_above_the_single_file_limit(self) -> None:
        """比單檔上限寬一點：multipart 的邊界、標頭與檔名也算在 body 裡。卡在剛好等於
        檔案上限的話，一個剛好 32MB 的合法上傳會被這一層擋掉，而訊息會說「超過 32MB」
        ——使用者看著自己 32MB 的檔案完全無法理解。"""
        from services.knowledge.uploads import MAX_UPLOAD_BYTES

        assert MAX_REQUEST_BYTES > MAX_UPLOAD_BYTES

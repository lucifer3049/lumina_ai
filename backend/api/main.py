"""FastAPI app factory —— 唯一對外 HTTP 入口（ADR-001）。

錯誤回應一律是 **RFC 9457 Problem Details**（09 §1.3），媒體型別
``application/problem+json``；組裝在 :func:`problem_response` 單點，
code → HTTP status 查 :data:`_HTTP_STATUS`（= 09 附錄 A）。

「一律」是指**四個入口全部接管**——只接 ``DomainError`` 會漏掉大半錯誤：

1. :class:`~core.exceptions.DomainError`：業務例外
2. ``RequestValidationError``：FastAPI 參數/body 驗證（否則回自己的 ``detail[]``）
3. ``HTTPException``：路由不存在、方法不允許（否則回 ``{"detail": "Not Found"}``）
4. ``Exception``：兜底（否則 Starlette 回純文字 Internal Server Error）

漏接不會有徵兆，所以由 ``tests/test_api_errors.py`` 逐個入口打過去驗證，
而不是靠讀碼確認。

02 §api 把 middleware 與 exception handler 分別放在 ``api/middleware/`` 與
``api/exception_handlers.py``。spike 階段全部留在本檔，Phase 0 隨租戶認證改造
一起拆——現在拆會讓 spike 的刪除範圍變得難以界定。

注意本檔**沒有** ``close_old_connections()`` 的 middleware。那是刻意的：
Django connection 是 thread-local，從 event loop 執行緒呼叫關不到 threadpool
上的連線。回收改在 ``core/db.py`` 的 ``run_orm()`` 內完成。詳見該檔 docstring。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.schemas.problem import PROBLEM_JSON, ProblemDetail
from core.exceptions import DomainError, ErrorCode
from core.tenant import reset_current_tenant_id, set_current_tenant_id

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"
_INTERNAL_ERROR_DETAIL = "內部錯誤，請提供 request_id 供追查"

# 09 附錄 A 的 HTTP 欄。**新增 code 必須同步這張表**——那視同 API 契約變更。
# SPIKE 範圍：只含 core/exceptions.py 已實作的 code；其餘於 Phase 0 隨 ErrorCode
# enum 一起補（刻意不先寫半套 enum，見 core/exceptions.py docstring）。
_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.INTERNAL_ERROR: 500,
}

# HTTPException（路由不存在、方法不允許…）的 status → 契約 code。
# 字典外的 status 不硬湊一個 code：見 problem_response 的 about:blank 分支。
_STATUS_CODE: dict[int, ErrorCode] = {404: ErrorCode.RESOURCE_NOT_FOUND}


def _problem_type(code: str) -> str:
    """RFC 9457 §3.1.1 的 ``type``。

    刻意用**相對 URI**（規格允許 URI reference），而不是 09 §1.3 範例裡的
    ``https://docs.example.com/errors/...``——那是文件的佔位符網域，我們還沒有
    錯誤說明文件站。寫死一個不存在的網域比相對 URI 更糟：client 真的會拿去連。
    Phase 0 有 docs 站之後改絕對 URI，屆時 base 走 Pydantic Settings（鐵則 9）。
    """
    return f"/errors/{code.lower().replace('_', '-')}"


def problem_response(
    *,
    status: int,
    detail: str,
    request_id: str,
    code: str | None = None,
    extensions: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """組出 RFC 9457 Problem Details 回應（09 §1.3）——**所有**錯誤的唯一出口。

    ``title`` 描述的是**錯誤類型**（同一 type 恆定），``detail`` 才是這一次發生的
    情況——RFC 9457 §3.1 的分工。``code`` / ``request_id`` 是 extension member
    （§3.2 允許），前者是 client 唯一該拿來做判斷的穩定值。

    ``code=None``（09 附錄 A 沒有對應碼，例如 405）走 RFC 9457 §4.2 的
    ``about:blank``：明示「語意就是 HTTP 狀態碼本身」。刻意不從 status 反推一個
    看起來像 code 的字串——那會讓契約字典多出沒經 review 的成員。
    """
    body: dict[str, Any] = {
        "type": _problem_type(code) if code else "about:blank",
        "title": code.replace("_", " ").capitalize() if code else HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "request_id": request_id,
    }
    if code:
        body["code"] = code
    if extensions:
        body.update(extensions)
    return JSONResponse(
        status_code=status,
        content=body,
        media_type=PROBLEM_JSON,
        headers={**(headers or {}), REQUEST_ID_HEADER: request_id},
    )


def _install_problem_schema(app: FastAPI) -> None:
    """把 ``ProblemDetail`` 註冊進 OpenAPI components。

    ``ERROR_RESPONSES`` 直接寫 ``$ref`` 而不給 ``model``（理由見
    ``api/schemas/problem.py``），代價是 FastAPI 不會自動註冊該 schema——
    產出的文件會有一堆指向不存在元件的 ``$ref``。這裡補上。

    先呼叫 ``app.openapi()`` 讓 FastAPI 走完自己的產生流程並快取，再就地補
    components；不自行呼叫 ``get_openapi()`` 是為了不用複製 FastAPI 那串參數
    （title/description/servers/separate_input_output_schemas…），那種複製會
    在升版時默默漂掉。

    副作用：schema 自此固定。``create_app()`` 之後再掛路由不會反映到
    ``/openapi.json``——正式路徑不會那樣做，測試若需要請重建 app。
    """
    schema = app.openapi()
    problem = ProblemDetail.model_json_schema(ref_template="#/components/schemas/{model}")
    schemas: dict[str, Any] = schema.setdefault("components", {}).setdefault("schemas", {})
    schemas.update(problem.pop("$defs", {}))  # FieldError 等巢狀模型
    schemas["ProblemDetail"] = problem


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


def create_app() -> FastAPI:
    app = FastAPI(title="Lumina spike (ADR-001 bridge)", version="0.1.0")

    @app.middleware("http")
    async def tenant_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """從 ``X-Tenant-Id`` 標頭取租戶。

        ⚠️ **SPIKE ONLY —— 這違反 ADR-002「不接受 client 自報 tenant_id」。**

        正式實作必須從已驗證的 JWT claim 取得租戶，client 送什麼都不採信。
        此處這樣寫的唯一理由是壓測需要在無認證的情況下切換租戶；Phase 0 接上
        認證後**必須刪除**這段，並改由 api/middleware/tenant_context.py 承擔。
        """
        raw = request.headers.get("X-Tenant-Id")
        token = None
        if raw:
            try:
                token = set_current_tenant_id(uuid.UUID(raw))
            except ValueError:
                return problem_response(
                    status=400,
                    # 這個 code 不在 09 附錄 A 的字典裡：本 middleware 是 spike
                    # 專用且 Phase 0 會整段刪除（見上方 ⚠️），刻意不為它污染契約字典。
                    code="INVALID_TENANT_ID",
                    detail="X-Tenant-Id 不是合法 UUID",
                    request_id=request_id_of(request),
                )
        try:
            return await call_next(request)
        finally:
            if token is not None:
                reset_current_tenant_id(token)

    @app.middleware("http")
    async def request_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """指派 request_id 並回寫成標頭，讓 client 回報的 id 對得上 log。

        **註冊順序有意義**：Starlette 的 user middleware 以 ``insert(0)`` 堆疊，
        後註冊者在外層。本 middleware 必須排在 ``tenant_middleware`` 之後宣告，
        才會比它先執行——否則 tenant_middleware 回的 400 拿不到同一個 id。
        """
        request_id = request_id_of(request)
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        """業務例外 → HTTP 的唯一轉換點（04 §B.1）。

        status 一律查 :data:`_HTTP_STATUS`（= 09 附錄 A 的 HTTP 欄），不做
        ``isinstance`` 特判——特判會讓對映散落在程式流程裡，與「字典是單一
        事實來源」的規則漂移。查不到的 code 一律當 500：未登錄的 code 是
        程式錯誤，寧可噪音也不要猜一個 4xx 把它藏起來。
        """
        status = _HTTP_STATUS.get(exc.code, 500)
        request_id = request_id_of(request)

        if status >= 500:
            # core/exceptions.py 對 INTERNAL_ERROR 的契約是「不洩細節，附 request_id」。
            # exc.details 會帶內部類別與方法名（如 SpikeItemRepository.get_queryset），
            # 那是 client 不該看到的實作結構——只寫進 log，回應收斂成通用敘述。
            # code 本身不遮蔽：它屬於公開字典，遮掉只會讓 client 無從分辨。
            logger.error(
                "%s | request_id=%s details=%s",
                exc,
                request_id,
                exc.details,
                exc_info=exc,
            )
            return problem_response(
                status=status,
                code=str(exc.code),
                detail=_INTERNAL_ERROR_DETAIL,
                request_id=request_id,
            )

        # 4xx 是使用者可修正的錯誤，details 屬於契約的一部分，照實回傳。
        # 註：VALIDATION_FAILED 走 errors[]（見 validation_error_handler），
        # 與此處的 details 是不同用途。
        return problem_response(
            status=status,
            code=str(exc.code),
            detail=exc.message,
            request_id=request_id,
            extensions={"details": exc.details} if exc.details else None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """FastAPI 參數/body 驗證失敗 → 422 VALIDATION_FAILED（09 附錄 A）。

        沒有這個 handler 時 FastAPI 會回自己的 ``{"detail":[...]}``——格式與
        Problem Details 完全不同，「統一錯誤格式」就只是名義上的統一。

        只取 ``loc`` 與 ``msg`` 組成 09 §1.3 的 ``errors[{field,message}]``；
        pydantic 的 ``input`` / ``ctx`` 刻意丟棄——那會把原始輸入與驗證器內部
        狀態原樣回吐。
        """
        errors = [
            {
                # loc[0] 是來源（query/body/path），欄位名從第二段起。
                "field": ".".join(str(part) for part in err["loc"][1:]) or str(err["loc"][0]),
                "message": err["msg"],
            }
            for err in exc.errors()
        ]
        return problem_response(
            status=422,
            code=str(ErrorCode.VALIDATION_FAILED),
            detail="請求參數未通過驗證",
            request_id=request_id_of(request),
            extensions={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """框架層 HTTPException（路由不存在、方法不允許…）→ Problem Details。

        ``exc.headers`` 必須傳遞下去：405 的 ``Allow``、429 的 ``Retry-After``
        都靠它，吞掉會讓回應不符合 HTTP 語意。
        """
        code = _STATUS_CODE.get(exc.status_code)
        return problem_response(
            status=exc.status_code,
            code=str(code) if code else None,
            detail=str(exc.detail),
            request_id=request_id_of(request),
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """兜底：任何未預期例外 → 500 Problem Details，細節只進 log。

        沒有這一層時 Starlette 的 ServerErrorMiddleware 會回純文字
        ``Internal Server Error``（DEBUG 下甚至回 traceback HTML）——那是格式
        破口，也是資訊洩漏破口。

        註：ServerErrorMiddleware 在送出本回應後**仍會重新 raise**，好讓
        ASGI server 記錄；測試端因此要用 ``raise_app_exceptions=False``。
        """
        request_id = request_id_of(request)
        logger.error("未預期例外 | request_id=%s", request_id, exc_info=exc)
        return problem_response(
            status=500,
            code=str(ErrorCode.INTERNAL_ERROR),
            detail=_INTERNAL_ERROR_DETAIL,
            request_id=request_id,
        )

    from api.v1.spike import router as spike_router

    app.include_router(spike_router, prefix="/api/v1")
    _install_problem_schema(app)
    return app

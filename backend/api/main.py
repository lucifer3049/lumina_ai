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

import time
import uuid
from contextvars import Token
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.schemas.problem import PROBLEM_JSON, ProblemDetail
from config.logging import bind_request_context, clear_request_context, get_logger
from config.settings.app_settings import get_app_settings
from core.exceptions import DomainError, ErrorCode
from core.tenant import reset_current_tenant_id, set_current_tenant_id

logger = get_logger(__name__)

ACCESS_EVENT = "http_request"

REQUEST_ID_HEADER = "X-Request-Id"
_INTERNAL_ERROR_DETAIL = "內部錯誤，請提供 request_id 供追查"

# 09 附錄 A 的 HTTP 欄。**新增 code 必須同步這張表**——那視同 API 契約變更。
# SPIKE 範圍：只含 core/exceptions.py 已實作的 code；其餘於 Phase 0 隨 ErrorCode
# enum 一起補（刻意不先寫半套 enum，見 core/exceptions.py docstring）。
_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.AUTH_INVALID_CREDENTIALS: 401,
    ErrorCode.AUTH_TOKEN_EXPIRED: 401,
    ErrorCode.AUTH_TOKEN_REVOKED: 401,
    ErrorCode.ACCOUNT_LOCKED: 423,
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


def _status_phrase(status: int) -> str:
    """狀態碼的人類可讀敘述，供無 ``code`` 時當 ``title``。

    ``HTTPStatus(499)`` 會丟 ``ValueError``——而這個查詢跑在 **exception handler
    內部**。處理器自己爆掉就沒有人接得住了：ServerErrorMiddleware 會把回應降級成
    純文字 ``Internal Server Error``，連狀態碼都從 499 變成 500，本檔開頭「四個
    入口全部接管」的保證在這條路徑上失效。

    Starlette 的 ``HTTPException`` 不限制狀態碼數字，499（nginx 慣例的 client
    closed request）、598 這類值第三方套件或自家中介層都可能用到，所以這不是
    理論上的邊界。後備值刻意只回 ``HTTP {status}``——沒有標準敘述時就不要編一個。
    """
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return f"HTTP {status}"


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
        "title": code.replace("_", " ").capitalize() if code else _status_phrase(status),
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
        # jsonable_encoder：``extensions`` 的內容（多半是 ``DomainError.details``）
        # 型別是 ``dict[str, Any]``，很自然會帶 UUID / datetime / Decimal。
        # JSONResponse.render() 是裸的 ``json.dumps``，遇到這些會 TypeError——
        # 而這行跑在 exception handler **內部**，處理器自己爆掉就沒人接得住：
        # ServerErrorMiddleware 會把本來的 404 降級成純文字 500。
        content=jsonable_encoder(body),
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


# ── SPIKE ONLY：以下三個名字在工作包 1A 接上認證後整組刪除 ──────────────
# （ADR-002 結案條件；改由 api/middleware/tenant_context.py 從已驗證的 JWT claim
#   取租戶。刪除範圍 = 這個區塊 ＋ request_context_middleware 內標註的 5 行。）

SPIKE_TENANT_HEADER = "X-Tenant-Id"


class _InvalidTenantHeaderError(Exception):
    """標頭存在但不是合法 UUID。

    不用 ``DomainError``：那條路會經 exception handler，而本例外產生於
    middleware——middleware 拋的例外到不了 handler（見 create_app 的
    ``request_context_middleware``），所以這裡只當作內部訊號用。
    """


def _bind_spike_tenant(request: Request) -> Token[uuid.UUID] | None:
    """從 ``X-Tenant-Id`` 標頭取租戶並綁定；回傳供還原用的 token。

    ⚠️ **SPIKE ONLY —— 這違反 ADR-002「不接受 client 自報 tenant_id」。**

    正式實作必須從已驗證的 JWT claim 取得租戶，client 送什麼都不採信。此處這樣
    寫的唯一理由是壓測需要在無認證的情況下切換租戶；因此只在
    ``enable_spike_endpoints`` 開啟時才會被呼叫，未開啟時客戶端自報的租戶標頭
    完全不生效。
    """
    raw = request.headers.get(SPIKE_TENANT_HEADER)
    if not raw:
        return None
    try:
        return set_current_tenant_id(uuid.UUID(raw))
    except ValueError as exc:
        raise _InvalidTenantHeaderError from exc


# ── SPIKE ONLY 區塊結束 ────────────────────────────────────────────


class RequestContextMiddleware:
    """請求層的追蹤 context、存取日誌、回應標頭，以及（spike）租戶綁定。

    **四件事合成一條 middleware 是刻意的**，前一版是三條互相依賴的
    ``BaseHTTPMiddleware``，代價是三個問題：短路的回應不會被記錄、順序約束只寫在
    註解裡沒有測試釘住、每條 middleware 每個請求都要建一組 anyio task group 加
    兩個 memory object stream（落在 B 組壓測的熱路徑上）。

    **為什麼是純 ASGI 而不是 ``@app.middleware("http")``**（1A-3 改）：
    ``BaseHTTPMiddleware`` 的 ``call_next`` 把下游丟到**另一個 task** 執行，而
    contextvars 的修改不會從子 task 回流到父 task。1A 之前這不成問題——租戶由本層
    自己（spike 標頭）設定；1A 之後租戶來自 route 層的認證 ``Depends``，那是下游，
    於是這裡讀到的永遠是空的，**每一筆存取日誌的 tenant_id 都會靜靜消失**
    （13 §3.2）。純 ASGI 的 ``await self.app(...)`` 就在同一個 task 裡，下游設定的
    contextvar 在它回來之後讀得到。

    ``path`` 刻意不含 query string：``?token=...`` 這類值會整串落地成明文
    （鐵則 9）。需要查參數時走 audit log，那裡有欄位級的遮罩政策。
    """

    def __init__(self, app: ASGIApp, *, enable_spike_endpoints: bool) -> None:
        self._app = app
        self._enable_spike_endpoints = enable_spike_endpoints

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive)
        request_id = request_id_of(request)
        started = time.perf_counter()

        # ── SPIKE ONLY：1A-5 刪除以下 5 行（見 _bind_spike_tenant）──
        tenant_token: Token[uuid.UUID] | None = None
        invalid_tenant_header = False
        if self._enable_spike_endpoints:
            try:
                tenant_token = _bind_spike_tenant(request)
            except _InvalidTenantHeaderError:
                invalid_tenant_header = True

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
            if invalid_tenant_header:
                response = problem_response(
                    status=400,
                    # 這個 code 不在 09 附錄 A 的字典裡：租戶標頭是 spike 專用且
                    # 會在 1A-5 整段刪除，刻意不為它污染契約字典。
                    code="INVALID_TENANT_ID",
                    detail=f"{SPIKE_TENANT_HEADER} 不是合法 UUID",
                    request_id=request_id,
                )
                await response(scope, receive, send_with_request_id)
            else:
                await self._app(scope, receive, send_with_request_id)
        except Exception:
            # 例外會往上冒到 ServerErrorMiddleware 才變成 500 回應，那已在本層之外。
            # 不在這裡補一筆就會出現「有錯誤 log、卻沒有對應的存取記錄」的缺口。
            emit(500)
            raise
        else:
            emit(status_seen)
        finally:
            if tenant_token is not None:
                reset_current_tenant_id(tenant_token)
            clear_request_context()


def create_app(*, enable_spike_endpoints: bool | None = None) -> FastAPI:
    """建立 FastAPI app。

    ``enable_spike_endpoints`` 控制 **spike 專用面**——``tenant_middleware``
    （從 ``X-Tenant-Id`` 標頭取租戶）與 ``/spike`` 路由。兩者無認證且違反
    ADR-002，未開啟時完全不掛：未認證的跨租戶讀取面不存在。

    ``None``（預設）時讀 ``AppSettings.enable_spike_endpoints``（環境變數
    ``ENABLE_SPIKE_ENDPOINTS``，預設 ``False``），所以正式部署一律關閉；
    壓測與測試以顯式參數開啟。錯誤處理（exception handler）不受此旗標影響，
    一律掛上——那段會活到正式版。

    「省略即讀設定、給值就完全不碰設定」與 ``config/logging.py`` 的
    ``configure_logging`` 是同一個約定，刻意保持一致：``AppSettings`` 有必填憑證
    （Redis / S3），只要測試顯式傳值就不需要 ``.env``。**測試請一律顯式傳參**，
    裸呼叫會讓結果隨本機環境變數而變（跑壓測時旗標正是開的）。
    """
    if enable_spike_endpoints is None:
        enable_spike_endpoints = get_app_settings().enable_spike_endpoints

    app = FastAPI(title="Lumina spike (ADR-001 bridge)", version="0.1.0")

    app.add_middleware(RequestContextMiddleware, enable_spike_endpoints=enable_spike_endpoints)

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
                "domain_error",
                code=str(exc.code),
                status=status,
                message=exc.message,
                details=exc.details,
                request_id=request_id,
                exc_info=exc,
            )
            return problem_response(
                status=status,
                code=str(exc.code),
                detail=_INTERNAL_ERROR_DETAIL,
                request_id=request_id,
            )

        # 4xx 記 WARNING 而非 ERROR（12 §1.1 等級紀律：ERROR = 需人看）。
        # 把使用者打錯 id 記成 ERROR 會讓告警噪音淹掉真正需要人看的事件；
        # 記 INFO 又和存取日誌重複、失去「單一租戶 403 暴增」這類可統計的訊號。
        logger.warning(
            "domain_error",
            code=str(exc.code),
            status=status,
            message=exc.message,
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

        5xx 的處理與 :func:`domain_error_handler` 是**同一個契約**（不洩細節、
        記 ERROR、附 request_id），不是巧合的重複：``HTTPException`` 的
        ``detail`` 由 raise 的那一方自由填寫，而第三方套件與未來的中介層會把
        上游端點、金鑰片段這類內容寫進去。原本這裡對所有狀態碼一律回吐
        ``exc.detail`` 且完全不記 log，於是 5xx 同時是資訊洩漏破口與觀測盲區
        ——故障期間 ERROR 級事件為零，告警不會響。
        """
        code = _STATUS_CODE.get(exc.status_code)
        request_id = request_id_of(request)

        if exc.status_code >= 500:
            logger.error(
                "http_exception",
                status=exc.status_code,
                detail=str(exc.detail),
                request_id=request_id,
                exc_info=exc,
            )
            return problem_response(
                status=exc.status_code,
                code=str(code) if code else None,
                detail=_INTERNAL_ERROR_DETAIL,
                request_id=request_id,
                headers=dict(exc.headers) if exc.headers else None,
            )

        return problem_response(
            status=exc.status_code,
            code=str(code) if code else None,
            detail=str(exc.detail),
            request_id=request_id,
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
        # request_id 明確傳值而非依賴 contextvars：本處理器由 ServerErrorMiddleware
        # 呼叫，位置在所有 user middleware **之外**，綁在內層的 context 到不了這裡。
        logger.error("unhandled_exception", request_id=request_id, exc_info=exc)
        return problem_response(
            status=500,
            code=str(ErrorCode.INTERNAL_ERROR),
            detail=_INTERNAL_ERROR_DETAIL,
            request_id=request_id,
        )

    # spike 路由（未認證、暴露租戶資料）只在旗標開啟時掛載；關閉時 app 只剩
    # 錯誤處理骨架，沒有任何可讀取租戶資料的端點。
    if enable_spike_endpoints:
        from api.v1.spike import router as spike_router

        app.include_router(spike_router, prefix="/api/v1")

    # 認證面永遠掛上（與 spike 旗標無關）：它是正式的租戶身分來源，
    # 而 spike 面是 1A-5 就要刪掉的暫時物。
    from api.v1.auth import router as auth_router

    app.include_router(auth_router, prefix="/api/v1")
    _install_problem_schema(app)
    return app

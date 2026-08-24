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
``api/exception_handlers.py``。**middleware 已於 2A-4 搬出去**（第二條 middleware
＝稽核進來的那一刻），本檔留下的是 exception handler 與 app factory；
``api/exception_handlers.py`` 的拆分同理留給下一次真的需要它的改動。

注意本檔**沒有** ``close_old_connections()`` 的 middleware。那是刻意的：
Django connection 是 thread-local，從 event loop 執行緒呼叫關不到 threadpool
上的連線。回收改在 ``core/db.py`` 的 ``run_orm()`` 內完成。詳見該檔 docstring。
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.background import SHUTDOWN_DRAIN_SECONDS, drain
from api.middleware.request_context import (
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
    request_id_of,
)
from api.schemas.problem import PROBLEM_JSON, ProblemDetail
from config.logging import get_logger
from core.exceptions import DomainError, ErrorCode
from core.object_storage import warm_up as warm_up_object_storage
from core.tasks import warm_up as warm_up_tasks

logger = get_logger(__name__)

# `REQUEST_ID_HEADER` / `request_id_of` 自 2A-4 起住在 api/middleware/request_context.py，
# 這裡再匯出：它們是 problem_response 的一部分，而錯誤契約的入口就是本檔。
__all__ = ["REQUEST_ID_HEADER", "create_app", "problem_response", "request_id_of"]

_INTERNAL_ERROR_DETAIL = "內部錯誤，請提供 request_id 供追查"

# 09 附錄 A 的 HTTP 欄。**新增 code 必須同步這張表**——那視同 API 契約變更。
# 目前只含 core/exceptions.py 已實作的 code；其餘隨對應工作包補上
# （刻意不先寫半套 enum，見 core/exceptions.py docstring）。
_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.AUTH_INVALID_CREDENTIALS: 401,
    ErrorCode.AUTH_TOKEN_EXPIRED: 401,
    ErrorCode.AUTH_TOKEN_REVOKED: 401,
    ErrorCode.ACCOUNT_LOCKED: 423,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.RESOURCE_CONFLICT: 409,
    # 續傳緩衝區過期（1D-4b）。09 附錄 A 標的就是 409：client 該做的是改抓最終訊息。
    ErrorCode.RESUME_EXPIRED: 409,
    ErrorCode.UPLOAD_TOO_LARGE: 413,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: 415,
    # AI provider（1C-1）。目前只有 worker 走得到（embedding），但 1D 的 chat 會讓
    # 它們直接面向請求——先登錄，免得那時漏掉而全部掉進 500。
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.MODEL_NOT_ENABLED: 422,
    ErrorCode.PROVIDER_UNAVAILABLE: 503,
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


def _retry_after_header(exc: DomainError) -> dict[str, str] | None:
    """帶了 `retry_after_seconds` 的例外要同時送 `Retry-After` 標頭（RFC 9110 §10.2.3）。

    **在單一出口做，不在各個例外類別做**：這是 details 與標頭之間的一條對映規則，
    散在各處的話，下一個帶重試時間的例外會忘了送標頭，而症狀是「client 的自動重試
    沒有生效」——沒有錯誤、沒有 log，只是重試策略退回它自己的預設值。

    涵蓋 `ServerBusyError`（429，二次架構審計 F-04）與 `AccountLockedError`（423，
    1A-3 起就帶著這個 detail 卻沒有標頭）。值必須是整數秒。
    """
    seconds = (exc.details or {}).get("retry_after_seconds")
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
        return None
    return {"Retry-After": str(seconds)}


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


def create_app() -> FastAPI:
    """建立 FastAPI app —— 唯一對外 HTTP 入口（ADR-001）。

    **沒有任何旗標參數是刻意的**（1A-5）。前一版有 ``enable_spike_endpoints``，
    控制一組無認證、從 ``X-Tenant-Id`` 標頭取租戶的壓測路由（ADR-002 的已知偏離）。
    那組東西連同旗標在本工作包整組刪除：租戶只有一個來源，就是已驗證的 JWT claim
    （``api/dependencies/auth.py``）。回歸守門在 ``tests/api/test_spike_removal.py``。
    """
    app = FastAPI(title="Lumina AI Knowledge Platform", version="0.1.0", lifespan=_lifespan)

    # 在函式內 import，同下方的 router：這兩條 middleware 經 service 認識 Django
    # model（body_limit 讀 uploads 的上限常數），而模組層 import 會讓
    # 「import api.main」本身要求 settings 已設定。body_limit 另有一個理由：它反過來
    # 也要 import 本模組的 `problem_response`。
    from api.middleware.audit import AuditMiddleware
    from api.middleware.body_limit import BodySizeLimitMiddleware

    # **順序有意義**：先掛的在內層（Starlette 由後往前包）。稽核要讀租戶
    # contextvar，而清掉它的是 RequestContextMiddleware 的 finally——稽核必須
    # 在它裡面。由 tests/unit/test_audit_registry.py 釘住，不是只寫在這裡。
    app.add_middleware(AuditMiddleware)
    # body 上限在稽核**外**、追蹤 context **內**：被擋下的請求連 body 都不解析
    # （所以不必進稽核那一層），但它仍然要有 request_id 與一筆存取日誌——413 若在
    # 日誌上完全看不見，「使用者說傳不上去」就查不出是被哪一道擋的。
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)

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
            # exc.details 會帶內部類別與方法名（如 UserRepository.get_queryset），
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
            headers=_retry_after_header(exc),
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

    from api.health import router as health_router
    from api.v1.analytics import router as analytics_router
    from api.v1.audit import router as audit_router
    from api.v1.auth import router as auth_router
    from api.v1.conversations import router as conversations_router
    from api.v1.knowledge import router as knowledge_router
    from api.v1.notifications import router as notifications_router
    from api.v1.rag import router as rag_router
    from api.v1.tenants import router as tenants_router
    from api.v1.users import router as users_router

    # **不帶 `/api/v1` 前綴**：探測端點屬於基礎設施契約，不是業務 API——版本化的
    # 是後者。編排器的探測路徑寫在 compose / K8s 的設定裡，跟著 API 版本走的話，
    # 每次改版都要同步改一份部署設定，而漏改的症狀是「容器一直被判定不健康」。
    # `include_in_schema=False`（見該 router）：它們也不該出現在給前端的 OpenAPI 裡。
    app.include_router(health_router)
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")
    app.include_router(tenants_router, prefix="/api/v1")
    app.include_router(knowledge_router, prefix="/api/v1")
    app.include_router(rag_router, prefix="/api/v1")
    app.include_router(conversations_router, prefix="/api/v1")
    app.include_router(analytics_router, prefix="/api/v1")
    app.include_router(audit_router, prefix="/api/v1")
    app.include_router(notifications_router, prefix="/api/v1")
    _install_problem_schema(app)

    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """啟動時在**背景**預熱外部 client；關機時等背景生成收工。

    兩者的第一次使用都很貴（boto3 建 client 要載入數百個描述檔，在 WSL2 掛載磁碟上
    實測 15.6 秒；Celery 要 import 整個套件並連上 broker），而那筆成本原本會落在重啟
    後的第一個上傳請求上——症狀是「偶爾有一次上傳非常慢」，只在剛部署完出現，看起來
    像儲存或網路的問題。

    用背景執行緒而不是 await：預熱不該擋住服務就緒（healthcheck 與讀取路徑都不需要
    它）。失敗只記 log——這裡沒有任何東西是啟動的必要條件。
    """
    threading.Thread(target=_warm_up_clients, name="warm-up", daemon=True).start()
    yield
    # 關機：等進行中的生成收工（11 §196、1D-4b）。不等的話，那些回答直接蒸發，而
    # 資料庫裡對應的訊息永遠停在 `streaming`——重整也不會變，因為沒有人會再去動它。
    unfinished = await drain(timeout_seconds=SHUTDOWN_DRAIN_SECONDS)
    if unfinished:
        logger.warning("shutdown_left_generations_unfinished", count=unfinished)


@cache
def _warm_up_clients() -> None:
    """預熱**一個行程只做一次**。

    `@cache` 在這裡不是省時間，是防數量：測試會反覆建立 app（每個 api 測試一次），
    沒有它的話每建一次就多一條 warm-up 執行緒與一條 broker 連線。
    """
    warm_up_object_storage()
    warm_up_tasks()

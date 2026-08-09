"""A 組正確性測試 —— API 錯誤路徑（api/main.py）。

補的是 test_bridge.py 沒涵蓋的一段：middleware 與 exception handler 這兩處
**會活到正式版**的程式碼。錯誤格式（09 附錄 A）與 500 不洩細節（core/exceptions.py
對 INTERNAL_ERROR 的契約）改壞了必須有紅燈，不能只靠人看。

全程不碰 DB：每條路徑都在查詢發出前就結束，所以不需要 ``db`` fixture。

**載具全部是本檔自掛的臨時路由**（1A-5 起）。上一版多數斷言掛在 ``/api/v1/spike/*``
與 ``X-Tenant-Id`` 上，而那些東西已隨 spike 面刪除。改成自掛路由不只是替換載具，
它讓這些測試不再與任何業務端點綁定：驗的是 handler 的行為，端點增刪不會誤傷。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Query
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.main import REQUEST_ID_HEADER, create_app
from api.schemas.problem import PROBLEM_JSON
from core.exceptions import ErrorCode, NotFoundError


def assert_problem_details(
    response: httpx.Response, *, status: int, code: str
) -> dict[str, object]:
    """斷言回應符合 RFC 9457 Problem Details（09 §1.3）並回傳 body。

    ``type``/``title``/``status``/``detail`` 是規格成員，``code``/``request_id``
    是 extension member；媒體型別必須是 application/problem+json——回 plain
    application/json 的話 client 端的 problem 解析器不會認得。
    """
    assert response.status_code == status
    assert response.headers["content-type"].startswith(PROBLEM_JSON)

    body: dict[str, object] = response.json()
    assert body["type"] == f"/errors/{code.lower().replace('_', '-')}"
    assert body["title"] == code.replace("_", " ").capitalize()
    assert body["status"] == status
    assert body["code"] == code
    assert body["detail"]
    assert body["request_id"] == response.headers[REQUEST_ID_HEADER]
    return body


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with _client(create_app()) as c:
        yield c


class TestDomainErrorMapping:
    """``domain_error_handler`` 的 status 映射與回應內容。"""

    async def test_missing_tenant_context_returns_opaque_500(self) -> None:
        """缺 TenantContext → 500，且**不得**把內部類別/方法名回給 client。

        ``TenantContextMissingError.details["operation"]`` 是
        ``UserRepository.get_queryset`` 這種實作結構，屬於資訊洩漏
        （core/exceptions.py：500 不洩細節，附 request_id）。

        例外由真的 Repository 產生（而非自己 raise 一個）：要驗的是實際會流到
        client 的那份 details，自己造的話 details 內容是測試自己寫的，斷言就變成
        在檢查自己。不需要 DB——`get_queryset` 在送出查詢之前就 raise 了。
        """
        from repositories.identity import UserRepository

        app = create_app()

        @app.get("/api/v1/_needs_tenant")
        async def _needs_tenant() -> None:
            UserRepository().get_queryset()

        async with _client(app) as client:
            response = await client.get("/api/v1/_needs_tenant")

        body = assert_problem_details(response, status=500, code=ErrorCode.INTERNAL_ERROR)
        assert "details" not in body, "500 回應不得帶 details"
        assert "UserRepository" not in response.text, "內部類別名洩漏到回應"
        assert "get_queryset" not in response.text, "內部方法名洩漏到回應"

    async def test_not_found_maps_to_404_from_code_dictionary(self) -> None:
        """``RESOURCE_NOT_FOUND`` → **404**，依 09 附錄 A 的 HTTP 欄。

        status 來自 code 字典而非 isinstance 特判——這條同時釘住「字典是單一
        事實來源」。用自掛的臨時路由：驗的是 handler 的對映，不是某支端點的行為。
        """
        app = create_app()

        @app.get("/api/v1/_raises_not_found")
        async def _raises() -> None:
            raise NotFoundError("找不到資源", details={"resource_id": "abc"})

        async with _client(app) as client:
            response = await client.get("/api/v1/_raises_not_found")

        body = assert_problem_details(response, status=404, code=ErrorCode.RESOURCE_NOT_FOUND)
        assert body["detail"] == "找不到資源"
        assert body["details"] == {"resource_id": "abc"}

    async def test_non_json_native_details_keep_the_original_status(self) -> None:
        """``details`` 帶 UUID / datetime 時仍須回原本的 4xx，不得降級成 500。

        ``DomainError.details`` 的型別是 ``dict[str, Any]``，而
        ``details={"resource_id": item_id}`` 帶一個 UUID 是最自然的寫法。
        ``JSONResponse.render()`` 是裸的 ``json.dumps``——TypeError 會在
        **exception handler 內部**炸開，ServerErrorMiddleware 於是把本來的 404
        降級成純文字 500：client 看到「伺服器錯誤」而不是「找不到資源」，而伺服器
        端只會多一筆 unhandled_exception，指不到真正的原因。
        """
        resource_id = uuid.uuid4()
        app = create_app()

        @app.get("/api/v1/_details_with_uuid")
        async def _raises() -> None:
            raise NotFoundError("找不到資源", details={"resource_id": resource_id})

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/v1/_details_with_uuid")

        body = assert_problem_details(response, status=404, code=ErrorCode.RESOURCE_NOT_FOUND)
        assert body["details"] == {"resource_id": str(resource_id)}


class TestEveryErrorEntryPointIsProblemJson:
    """**格式統一的反查測試**：打遍每一個會產生錯誤回應的入口。

    這個 class 存在的理由，是我上一版只註冊了 ``DomainError`` 的 handler 就在
    docstring 寫「錯誤回應一律是 RFC 9457」——實際上 ``?limit=999`` 回的是
    FastAPI 自己的 ``{"detail":[...]}``。逐條列舉「我寫過的路徑」永遠測不出
    「我沒想到的路徑」；這裡改成由入口清單反推，漏接一個就紅燈。
    """

    async def test_validation_error_is_problem_json(self) -> None:
        """FastAPI 參數驗證（``RequestValidationError``）→ 422 VALIDATION_FAILED。"""
        app = create_app()

        @app.get("/api/v1/_bounded")
        async def _bounded(limit: int = Query(20, ge=1, le=100)) -> dict[str, int]:
            return {"limit": limit}

        async with _client(app) as client:
            response = await client.get("/api/v1/_bounded?limit=999")

        body = assert_problem_details(response, status=422, code=ErrorCode.VALIDATION_FAILED)
        assert body["errors"] == [
            {"field": "limit", "message": "Input should be less than or equal to 100"}
        ]
        assert "ctx" not in response.text, "pydantic 驗證器內部狀態不該回給 client"

    async def test_unknown_route_is_problem_json(self, client: httpx.AsyncClient) -> None:
        """``HTTPException``（路由不存在）→ 404 RESOURCE_NOT_FOUND，不是 Starlette 預設格式。"""
        response = await client.get("/api/v1/does-not-exist")

        assert_problem_details(response, status=404, code=ErrorCode.RESOURCE_NOT_FOUND)

    async def test_status_without_contract_code_uses_about_blank(self) -> None:
        """09 附錄 A 沒有對應碼的 status（405）→ ``about:blank``，且不憑空造 code。"""
        app = create_app()

        @app.get("/api/v1/_get_only")
        async def _get_only() -> dict[str, bool]:
            return {"ok": True}

        async with _client(app) as client:
            response = await client.post("/api/v1/_get_only")

        assert response.status_code == 405
        assert response.headers["content-type"].startswith(PROBLEM_JSON)
        body = response.json()
        assert body["type"] == "about:blank"
        assert body["title"] == "Method Not Allowed"
        assert "code" not in body, "不得為字典外的 status 憑空產生 code"
        assert response.headers["allow"], "405 的 Allow 標頭被吞掉了"

    async def test_non_standard_status_is_problem_json(self) -> None:
        """非標準狀態碼（499）也必須是 Problem Details，且不得讓 handler 自己爆掉。

        ``problem_response`` 在無 code 時取 ``HTTPStatus(status).phrase``，而
        ``HTTPStatus(499)`` 直接丟 ``ValueError``。那行跑在 **exception handler
        內部**——處理器自己爆掉就沒有人接得住了，ServerErrorMiddleware 會回純文字
        ``Internal Server Error``，本檔開頭「四個入口全部接管」的保證在這條路徑
        上失效，而且失效時回的還是 500（狀態碼也錯）。

        Starlette 的 ``HTTPException`` 不限制狀態碼數字，499（nginx 慣例的
        client closed request）、598 這類值第三方套件或自家中介層都可能用到。
        """
        app = create_app()

        @app.get("/api/v1/_nonstandard_status")
        async def _nonstandard() -> None:
            raise StarletteHTTPException(status_code=499, detail="client closed request")

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/v1/_nonstandard_status")

        assert response.status_code == 499, "handler 內部爆掉會被降級成 500"
        assert response.headers["content-type"].startswith(PROBLEM_JSON)

        body = response.json()
        assert body["type"] == "about:blank"
        assert body["status"] == 499
        assert body["title"], "title 是 RFC 9457 必填成員，不得為空"
        assert "code" not in body, "不得為字典外的 status 憑空產生 code"
        assert body["request_id"] == response.headers[REQUEST_ID_HEADER]

    async def test_5xx_http_exception_does_not_echo_detail(self) -> None:
        """5xx ``HTTPException`` 的 ``detail`` 不得回給 client。

        ``detail`` 由 raise 的那一方自由填寫，而第三方套件與未來的中介層會把上游
        端點、金鑰片段這類內容寫進去。原本這個 handler 對**所有**狀態碼一律回吐
        ``exc.detail``，於是 5xx 成了資訊洩漏破口——與 ``domain_error_handler``
        對 5xx 的處理（收斂成通用敘述）不一致，而不一致的那半沒有測試。

        對照組是 4xx（``test_status_without_contract_code_uses_about_blank`` 的 405
        與下面的 404）：4xx 的 detail 是使用者可修正的資訊，必須照實回傳。
        """
        app = create_app()

        @app.get("/api/v1/_upstream_5xx")
        async def _upstream() -> None:
            raise StarletteHTTPException(
                status_code=503,
                detail="upstream http://minio:9000 rejected key AKIA-LEAKED-EXAMPLE",
            )

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/v1/_upstream_5xx")

        assert response.status_code == 503, "狀態碼必須保留，只收斂 detail"
        assert response.headers["content-type"].startswith(PROBLEM_JSON)
        assert "AKIA-LEAKED-EXAMPLE" not in response.text, "金鑰片段洩漏到回應"
        assert "minio:9000" not in response.text, "內部端點洩漏到回應"

    async def test_unhandled_exception_is_problem_json(self) -> None:
        """兜底 handler：未預期例外 → 500 Problem Details，不洩內部細節。

        ``raise_app_exceptions=False``：ServerErrorMiddleware 送出回應後仍會
        重新 raise（好讓 ASGI server 記錄），這裡要驗的是送出去的那份回應。
        """
        app = create_app()

        @app.get("/api/v1/_boom")
        async def _boom() -> None:
            raise RuntimeError("內部爆炸細節不該外流")

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/v1/_boom")

        body = assert_problem_details(response, status=500, code=ErrorCode.INTERNAL_ERROR)
        assert "details" not in body
        assert "內部爆炸細節不該外流" not in response.text
        assert "RuntimeError" not in response.text


# 註（1A-5）：這裡原有一組 ``TestDiagnosticsEndpointDoesNotLeakTopology``，驗
# ``/spike/healthz`` 經 HTTP 出去的內容不含 DB 主機與埠。那支端點隨 spike 面刪除，
# 而回報內容本身的守門在 ``tests/unit/test_orm_knobs.py``（那一層刻意訂在
# ``core.db.orm_runtime_knobs()`` 上，正是為了活得比端點久）。HTTP 那半要等
# 11 §4.2 的正式 ``/healthz`` 落地時重建——**建那支端點時必須一併把這組測試補回來**，
# 否則「診斷端點會長大」這件事就沒有人擋了（它無認證，加什麼都不會有徵兆）。


class TestOpenApiDeclaresErrorContract:
    """A3：錯誤契約必須進 OpenAPI，否則前端 codegen 產不出錯誤型別（09 §4、鐵則 10）。"""

    def test_every_operation_declares_problem_responses(self) -> None:
        schema = create_app().openapi()
        problem_ref = "#/components/schemas/ProblemDetail"

        for path, operations in schema["paths"].items():
            for method, operation in operations.items():
                responses = operation["responses"]
                for status in ("400", "404", "422", "500"):
                    assert status in responses, f"{method.upper()} {path} 未宣告 {status}"
                    content = responses[status]["content"]
                    assert PROBLEM_JSON in content, (
                        f"{method.upper()} {path} 的 {status} 媒體型別不是 {PROBLEM_JSON}"
                    )
                    assert content[PROBLEM_JSON]["schema"]["$ref"] == problem_ref

    def test_problem_schema_carries_contract_fields(self) -> None:
        schema = create_app().openapi()
        properties = schema["components"]["schemas"]["ProblemDetail"]["properties"]

        # RFC 9457 §3.1 成員 + 09 §1.3 的 extension member
        for field in ("type", "title", "status", "detail", "request_id", "code", "errors"):
            assert field in properties, f"ProblemDetail 缺欄位 {field}"


class TestRequestId:
    """載具用不存在的路徑：404 同樣走 problem_response，body 一樣帶 request_id。"""

    async def test_request_id_header_matches_body(self, client: httpx.AsyncClient) -> None:
        """回應標頭與 body 的 request_id 必須是同一個，否則使用者回報的 id 對不上 log。"""
        response = await client.get("/api/v1/does-not-exist")

        assert response.headers[REQUEST_ID_HEADER] == response.json()["request_id"]

    async def test_request_id_is_not_taken_from_client(self, client: httpx.AsyncClient) -> None:
        """client 自送的 X-Request-Id 不採信——未驗證輸入不進 log 追蹤欄位。"""
        response = await client.get(
            "/api/v1/does-not-exist",
            headers={REQUEST_ID_HEADER: "injected-by-client"},
        )

        assert response.headers[REQUEST_ID_HEADER] != "injected-by-client"
        assert response.json()["request_id"] != "injected-by-client"

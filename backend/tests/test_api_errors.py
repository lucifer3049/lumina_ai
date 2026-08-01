"""A 組正確性測試 —— API 錯誤路徑（api/main.py）。

補的是 test_bridge.py 沒涵蓋的一段：middleware 與 exception handler 這兩處
**會活到正式版**的程式碼。錯誤格式（09 附錄 A）與 500 不洩細節（core/exceptions.py
對 INTERNAL_ERROR 的契約）改壞了必須有紅燈，不能只靠人看。

全程不碰 DB：三條路徑都在查詢發出前就結束，所以不需要 ``db`` fixture。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.main import REQUEST_ID_HEADER, create_app
from api.schemas.problem import PROBLEM_JSON
from core.exceptions import ErrorCode, NotFoundError
from tests.conftest import TENANT_A


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


class TestTenantHeaderValidation:
    """``X-Tenant-Id`` 解析失敗必須是 400，且不得往下走到 Service。"""

    async def test_malformed_tenant_id_returns_400(self, client: httpx.AsyncClient) -> None:
        response = await client.get(
            "/api/v1/spike/items",
            headers={"X-Tenant-Id": "not-a-uuid"},
        )

        # assert_problem_details 內含 request_id 與標頭一致的斷言，順帶釘住
        # middleware 的註冊順序：request_id 必須比 tenant_middleware 先跑，
        # 否則這條 400 會拿到一個標頭上看不到的 id（見 api/main.py 的說明）。
        assert_problem_details(response, status=400, code="INVALID_TENANT_ID")

    async def test_valid_tenant_id_passes_middleware(
        self,
        client: httpx.AsyncClient,
        two_tenants: tuple[uuid.UUID, uuid.UUID],
    ) -> None:
        """對照組：合法 UUID 應一路穿到 DB 並只拿到該租戶的資料。

        沒有這條對照，上面那條 400 也可能是「什麼都擋」造成的。順帶把
        middleware → service → run_orm → repository 這條完整路徑走一次 HTTP。
        """
        response = await client.get(
            "/api/v1/spike/items",
            headers={"X-Tenant-Id": str(TENANT_A)},
        )

        assert response.status_code == 200
        assert {row["title"] for row in response.json()} == {"a-0", "a-1", "a-2"}


class TestDomainErrorMapping:
    """``domain_error_handler`` 的 status 映射與回應內容。"""

    async def test_missing_tenant_context_returns_opaque_500(
        self, client: httpx.AsyncClient
    ) -> None:
        """缺 TenantContext → 500，且**不得**把內部類別/方法名回給 client。

        ``TenantContextMissingError.details["operation"]`` 是
        ``SpikeItemRepository.get_queryset`` 這種實作結構，屬於資訊洩漏
        （core/exceptions.py：500 不洩細節，附 request_id）。
        """
        response = await client.get("/api/v1/spike/items")  # 刻意不帶 X-Tenant-Id

        body = assert_problem_details(response, status=500, code=ErrorCode.INTERNAL_ERROR)
        assert "details" not in body, "500 回應不得帶 details"
        assert "SpikeItemRepository" not in response.text, "內部類別名洩漏到回應"
        assert "get_queryset" not in response.text, "內部方法名洩漏到回應"

    async def test_not_found_maps_to_404_from_code_dictionary(self) -> None:
        """``RESOURCE_NOT_FOUND`` → **404**，依 09 附錄 A 的 HTTP 欄。

        status 來自 code 字典而非 isinstance 特判——這條同時釘住「字典是單一
        事實來源」。spike 沒有會 raise ``NotFoundError`` 的端點，所以在測試用的
        app 實例上掛一條臨時路由；驗的是 handler 的對映，不是某支端點的行為。
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


class TestEveryErrorEntryPointIsProblemJson:
    """**格式統一的反查測試**：打遍每一個會產生錯誤回應的入口。

    這個 class 存在的理由，是我上一版只註冊了 ``DomainError`` 的 handler 就在
    docstring 寫「錯誤回應一律是 RFC 9457」——實際上 ``?limit=999`` 回的是
    FastAPI 自己的 ``{"detail":[...]}``。逐條列舉「我寫過的路徑」永遠測不出
    「我沒想到的路徑」；這裡改成由入口清單反推，漏接一個就紅燈。
    """

    async def test_validation_error_is_problem_json(self, client: httpx.AsyncClient) -> None:
        """FastAPI 參數驗證（``RequestValidationError``）→ 422 VALIDATION_FAILED。"""
        response = await client.get("/api/v1/spike/items?limit=999")

        body = assert_problem_details(response, status=422, code=ErrorCode.VALIDATION_FAILED)
        assert body["errors"] == [
            {"field": "limit", "message": "Input should be less than or equal to 100"}
        ]
        assert "ctx" not in response.text, "pydantic 驗證器內部狀態不該回給 client"

    async def test_unknown_route_is_problem_json(self, client: httpx.AsyncClient) -> None:
        """``HTTPException``（路由不存在）→ 404 RESOURCE_NOT_FOUND，不是 Starlette 預設格式。"""
        response = await client.get("/api/v1/does-not-exist")

        assert_problem_details(response, status=404, code=ErrorCode.RESOURCE_NOT_FOUND)

    async def test_status_without_contract_code_uses_about_blank(
        self, client: httpx.AsyncClient
    ) -> None:
        """09 附錄 A 沒有對應碼的 status（405）→ ``about:blank``，且不憑空造 code。"""
        response = await client.post("/api/v1/spike/items")

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
    async def test_request_id_header_matches_body(self, client: httpx.AsyncClient) -> None:
        """回應標頭與 body 的 request_id 必須是同一個，否則使用者回報的 id 對不上 log。"""
        response = await client.get("/api/v1/spike/items")

        assert response.headers[REQUEST_ID_HEADER] == response.json()["request_id"]

    async def test_request_id_is_not_taken_from_client(self, client: httpx.AsyncClient) -> None:
        """client 自送的 X-Request-Id 不採信——未驗證輸入不進 log 追蹤欄位。"""
        response = await client.get(
            "/api/v1/spike/items",
            headers={REQUEST_ID_HEADER: "injected-by-client"},
        )

        assert response.headers[REQUEST_ID_HEADER] != "injected-by-client"
        assert response.json()["request_id"] != "injected-by-client"

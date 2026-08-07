"""A 組正確性測試 —— API 錯誤路徑（api/main.py）。

補的是 test_bridge.py 沒涵蓋的一段：middleware 與 exception handler 這兩處
**會活到正式版**的程式碼。錯誤格式（09 附錄 A）與 500 不洩細節（core/exceptions.py
對 INTERNAL_ERROR 的契約）改壞了必須有紅燈，不能只靠人看。

全程不碰 DB：三條路徑都在查詢發出前就結束，所以不需要 ``db`` fixture。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Iterator

import httpx
import pytest
from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.main import REQUEST_ID_HEADER, create_app
from api.schemas.problem import PROBLEM_JSON
from config.settings.app_settings import get_app_settings
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
    # 本檔驗的錯誤路徑多以 /spike 端點與 X-Tenant-Id 為載具，故顯式開啟 spike 面。
    async with _client(create_app(enable_spike_endpoints=True)) as c:
        yield c


@pytest.fixture
def spike_flag_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str | None], None]]:
    """設定 ``ENABLE_SPIKE_ENDPOINTS``（``None`` = 移除）並清掉 lru_cache。

    快取**前後都要清**：不清前面會讀到別條測試建好的 settings（本測試等於沒設值），
    不清後面則把測試用的旗標留給後續測試。兩種殘留都不會有錯誤訊息，只會讓
    「預設關閉」這個斷言在某些執行順序下變成假綠燈。
    """

    def _set(value: str | None) -> None:
        if value is None:
            monkeypatch.delenv("ENABLE_SPIKE_ENDPOINTS", raising=False)
        else:
            monkeypatch.setenv("ENABLE_SPIKE_ENDPOINTS", value)
        get_app_settings.cache_clear()

    try:
        yield _set
    finally:
        get_app_settings.cache_clear()


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


class TestSpikeSurfaceOffByDefault:
    """回歸：spike 面預設關閉——未認證跨租戶讀取的攻擊面不存在（ADR-002）。

    修復前：``tenant_middleware`` 無條件掛載且從 client 的 ``X-Tenant-Id`` 取租戶，
    ``/api/v1/spike/items`` 也無條件掛載，任何人帶一個租戶 id 即可讀該租戶資料。
    修復後：``enable_spike_endpoints`` 預設 False（正式部署），兩者都不掛。
    """

    async def test_spike_route_absent_when_disabled(self) -> None:
        """旗標關閉時 /spike 路由不存在——回 404，而非吐資料。"""
        async with _client(create_app(enable_spike_endpoints=False)) as client:
            response = await client.get(
                "/api/v1/spike/items",
                headers={"X-Tenant-Id": str(TENANT_A)},
            )

        # 路由未掛載 → 走 HTTPException handler 的 404，X-Tenant-Id 完全不生效。
        assert_problem_details(response, status=404, code=ErrorCode.RESOURCE_NOT_FOUND)

    async def test_client_tenant_header_not_bound_when_disabled(self) -> None:
        """旗標關閉時 tenant_middleware 未註冊：client 自報的租戶不進 context。

        掛一條會回讀 TenantContext 的臨時路由；帶了 X-Tenant-Id 也應看不到租戶，
        證明未認證的 client 無法設定租戶 context。
        """
        from core.tenant import try_get_current_tenant_id

        app = create_app(enable_spike_endpoints=False)

        @app.get("/api/v1/_tenant_probe")
        async def _probe() -> dict[str, str | None]:
            tid = try_get_current_tenant_id()
            return {"tenant_id": str(tid) if tid else None}

        async with _client(app) as client:
            response = await client.get(
                "/api/v1/_tenant_probe",
                headers={"X-Tenant-Id": str(TENANT_A)},
            )

        assert response.status_code == 200
        assert response.json()["tenant_id"] is None, "未認證的 X-Tenant-Id 不該進 context"

    async def test_disabled_when_env_flag_absent(
        self, spike_flag_env: Callable[[str | None], None]
    ) -> None:
        """不帶參數的 ``create_app()``——即 config/asgi.py 的正式路徑——預設關閉。

        上面兩條驗的是**參數**，這條驗的是**部署實際走的那條路**：環境變數沒設時
        整條鏈（env → ``AppSettings.enable_spike_endpoints`` → ``create_app``）必須
        收斂到關閉。刻意用 delenv 而非設 ``"false"``：設值會連 ``AppSettings`` 的
        欄位預設一起繞過，欄位被改成 ``True`` 時不會有紅燈。
        """
        spike_flag_env(None)

        async with _client(create_app()) as client:
            response = await client.get(
                "/api/v1/spike/items",
                headers={"X-Tenant-Id": str(TENANT_A)},
            )

        assert_problem_details(response, status=404, code=ErrorCode.RESOURCE_NOT_FOUND)

    async def test_env_flag_is_actually_read(
        self, spike_flag_env: Callable[[str | None], None]
    ) -> None:
        """反向：env 設 true 時 spike 面掛得起來。

        變異驗證用——少了這條，把 ``create_app`` 的 settings 分支寫死成 False
        也會全綠，上一條就只是在測一個永遠成立的常數。

        用 OpenAPI ``paths`` 而不是走訪 ``app.routes``：include_router 進來的路由在
        ``app.routes`` 裡是一個 ``_IncludedRouter`` 包裝物件，沒有 ``path`` 屬性，
        照 path 比對會恆為 False（= 假紅燈）。也不打 HTTP，那條會連 DB。
        """
        spike_flag_env("true")

        assert "/api/v1/spike/items" in create_app().openapi()["paths"]


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


class TestDiagnosticsEndpointDoesNotLeakTopology:
    """``/spike/healthz`` 是 repo 裡唯一的 diagnostics endpoint，且**無認證**。

    放在本檔的理由：本檔的主題是「回應不得夾帶內部細節」（500 handler 那幾條驗的
    是同一件事），只是這條不走錯誤路徑。鍵集合本身的守門在
    ``tests/unit/test_orm_knobs.py``——那一層活得比 spike 面久（1A 會整組刪除它）；
    這裡驗的是**經 HTTP 出去的東西**與那一層一致，也就是 controller 沒有自己加料。
    """

    async def test_healthz_exposes_only_the_measurement_knobs(
        self, client: httpx.AsyncClient
    ) -> None:
        from core.db import orm_runtime_knobs

        response = await client.get("/api/v1/spike/healthz")

        assert response.status_code == 200
        assert response.json() == orm_runtime_knobs(), "controller 自行組了回應內容"

    async def test_healthz_does_not_expose_db_host_or_port(self, client: httpx.AsyncClient) -> None:
        """前一版回報 ``db_host`` / ``db_port``——無認證端點上的內部拓撲（鐵則 9）。"""
        from django.conf import settings

        body = await client.get("/api/v1/spike/healthz")
        text = body.text
        database = settings.DATABASES["default"]

        assert "db_host" not in text
        assert "db_port" not in text
        assert str(database["HOST"]) not in text
        assert str(database["PORT"]) not in text


class TestOpenApiDeclaresErrorContract:
    """A3：錯誤契約必須進 OpenAPI，否則前端 codegen 產不出錯誤型別（09 §4、鐵則 10）。"""

    def test_every_operation_declares_problem_responses(self) -> None:
        schema = create_app(enable_spike_endpoints=True).openapi()
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
        schema = create_app(enable_spike_endpoints=True).openapi()
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

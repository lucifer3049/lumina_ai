"""驗收測試 —— 請求層的日誌關聯（12 §1.1 Correlation）。

test_logging.py 驗的是 logger 本身；這裡驗的是**每個 HTTP 請求都被打上同一個
request_id，且回應標頭與 log 對得起來**——client 回報一個 id，維運要能靠它撈到
那次請求的全部事件，這是 request_id 存在的唯一理由。

三件事沒有測試就不會有症狀：

1. 存取日誌漏掉某條路徑（例如錯誤路徑）——平時看不出來，出事時剛好沒 log。
2. context 跨請求殘留——contextvars 沒清乾淨時，A 請求的 tenant_id 會出現在
   B 請求的 log 上，那比沒有 log 更糟（誤導追查方向）。
3. query string 原樣入 log——`?token=...` 這類值會直接落地成明文（鐵則 9）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.main import REQUEST_ID_HEADER, create_app
from config.logging import configure_logging
from core.exceptions import NotFoundError
from core.tenant import set_current_tenant_id
from tests.conftest import TENANT_A

ACCESS_EVENT = "http_request"


def _log_lines(captured: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in captured.splitlines() if line.strip()]


def _access_logs(captured: str) -> list[dict[str, Any]]:
    return [e for e in _log_lines(captured) if e.get("event") == ACCESS_EVENT]


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


def _app_with_tenant_probe() -> FastAPI:
    """掛一條會在 route 內設定租戶的路由，模擬 1A-3 之後認證 ``Depends`` 的形狀。

    ``/scoped`` 設租戶、``/plain`` 不設，兩者搭配即可驗「租戶綁得到」與「不跨請求
    殘留」——而且完全不依賴任何業務端點。
    """
    app = create_app()

    async def bind_tenant() -> None:
        """**必須是 async**，與 `api/dependencies/auth.py` 的形狀一致。

        同步 dependency 會被 FastAPI 丟到 threadpool 執行，contextvar 設在那條
        執行緒的 context 副本上，回不到主 task——存取日誌因此讀不到租戶。這不是
        測試的技術細節：真正的認證 dependency 若哪天被改成同步的，log 的
        tenant_id 就會再次靜靜消失，而這條測試會抓到。
        """
        set_current_tenant_id(TENANT_A)

    @app.get("/scoped", dependencies=[Depends(bind_tenant)])
    async def scoped() -> dict[str, str]:
        return {"ok": "true"}

    @app.get("/plain")
    async def plain() -> dict[str, str]:
        return {"ok": "true"}

    return app


@pytest.fixture
async def client(capsys: pytest.CaptureFixture[str]) -> AsyncIterator[httpx.AsyncClient]:
    """在 capsys 生效後才設定 logging——handler 綁的是當下的 sys.stdout。"""
    configure_logging(level="INFO", fmt="json")
    async with _client(_app_with_tenant_probe()) as c:
        yield c


class TestAccessLog:
    async def test_every_request_emits_one_access_log(
        self, client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        response = await client.get("/api/v1/does-not-exist")

        logs = _access_logs(capsys.readouterr().out)

        assert len(logs) == 1
        event = logs[0]
        assert event["method"] == "GET"
        assert event["path"] == "/api/v1/does-not-exist"
        assert event["status"] == response.status_code
        assert isinstance(event["duration_ms"], (int, float))

    async def test_error_path_is_also_logged(
        self, client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """404 這種**沒有走到 route 函式**的請求同樣要留下存取記錄。

        本檔開頭第 1 條說的「存取日誌漏掉某條路徑」就是這類：回應 body 帶著
        request_id，log 裡卻一筆都沒有——使用者回報 id 撈不到東西，而 4xx 的計數
        與延遲分位數會系統性少算這一整類請求。
        """
        response = await client.get("/api/v1/does-not-exist")

        logs = _access_logs(capsys.readouterr().out)

        assert len(logs) == 1, "未進入 route 的回應沒有存取記錄"
        assert logs[0]["status"] == 404
        assert logs[0]["request_id"] == response.headers[REQUEST_ID_HEADER]

    async def test_request_id_matches_response_header(
        self, client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """對外可見的 id 與 log 內的 id 必須是同一個，否則 client 回報的 id 撈不到東西。"""
        response = await client.get("/api/v1/does-not-exist")

        assert (
            _access_logs(capsys.readouterr().out)[0]["request_id"]
            == (response.headers[REQUEST_ID_HEADER])
        )

    async def test_query_string_is_not_logged(
        self, client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        await client.get("/plain?limit=20&token=super-secret")

        out = capsys.readouterr().out

        assert "super-secret" not in out
        assert _access_logs(out)[0]["path"] == "/plain"

    async def test_context_does_not_leak_between_requests(
        self, client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """第二個請求沒設租戶，就不該看到第一個請求的 tenant_id。

        contextvars 沒清乾淨時，A 請求的租戶會出現在 B 請求的 log 上——那比沒有
        log 更糟，它會把追查方向指到無關的租戶去。
        """
        await client.get("/scoped")
        await client.get("/plain")

        first, second = _access_logs(capsys.readouterr().out)

        assert first["tenant_id"] == str(TENANT_A), "前提不成立：第一個請求沒綁到租戶"
        assert first["request_id"] != second["request_id"]
        assert second.get("tenant_id") is None


class TestTenantBinding:
    """租戶在 **route 層**被設定時，存取日誌仍然帶得到 tenant_id（13 §3.2）。

    缺口長這樣：租戶從已驗證的 JWT claim 取得，而 FastAPI 的慣用形狀是
    ``Depends``——那在 **route 函式內**執行，比所有 middleware 都晚。1A 之前的做法
    是 middleware 進入時對 contextvar 取一次快照再綁進 log context，那個時間點租戶
    還是空的，於是**每一筆 log 的 tenant_id 都會靜靜消失**，而 12 §1.1 把它列為
    標準欄位，「某個租戶錯誤暴增」這類查詢全靠它。

    處置是改成 emit 時才讀 contextvar 的 structlog processor：不管租戶是由
    middleware、``Depends`` 還是背景任務設進去的，都一樣抓得到。
    """

    async def test_tenant_set_by_a_route_dependency_reaches_the_access_log(
        self, client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        await client.get("/scoped")

        assert _access_logs(capsys.readouterr().out)[0]["tenant_id"] == str(TENANT_A)


class TestErrorCorrelation:
    """錯誤 log 與回應必須共用同一個 request_id（api/main.py 的 500 契約）。"""

    async def test_unhandled_error_log_shares_request_id(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="INFO", fmt="json")
        app = create_app()

        @app.get("/boom")
        async def boom() -> None:
            raise RuntimeError("kaboom")

        async with _client(app) as client:
            response = await client.get("/boom")

        request_id = response.json()["request_id"]
        events = _log_lines(capsys.readouterr().out)
        error_events = [e for e in events if e["level"] == "error"]

        assert error_events, "500 必須留下 ERROR 級 log，否則回應裡的 request_id 撈不到細節"
        assert all(e["request_id"] == request_id for e in error_events)
        assert "kaboom" in error_events[0]["exception"]

    async def test_5xx_http_exception_log_shares_request_id(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """5xx ``HTTPException`` 必須留 ERROR 級 log 且共用同一個 request_id。

        回應那半（不回吐 ``exc.detail``）由 test_api_errors.py 驗；這裡驗觀測那半：
        原本這條路徑完全不記 log，於是「上游 503」在故障期間產生**零筆** ERROR
        事件——告警不會響，而回應裡給使用者的 request_id 也撈不到任何細節。
        """
        configure_logging(level="INFO", fmt="json")
        app = create_app()

        @app.get("/upstream")
        async def upstream() -> None:
            raise StarletteHTTPException(status_code=503, detail="upstream rejected")

        async with _client(app) as client:
            response = await client.get("/upstream")

        request_id = response.json()["request_id"]
        error_events = [e for e in _log_lines(capsys.readouterr().out) if e["level"] == "error"]

        assert error_events, "5xx 未留 ERROR log → 故障期間告警不會響"
        assert all(e["request_id"] == request_id for e in error_events)

    async def test_domain_error_4xx_is_not_error_level(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """4xx 是使用者可修正的錯誤，不該進 ERROR（12 §1.1 等級紀律：ERROR = 需人看）。

        把 404 記成 ERROR 會讓告警噪音淹掉真正需要人看的事件。
        """
        configure_logging(level="INFO", fmt="json")
        app = create_app()

        @app.get("/missing")
        async def missing() -> None:
            raise NotFoundError("找不到文件")

        async with _client(app) as client:
            await client.get("/missing")

        assert not [e for e in _log_lines(capsys.readouterr().out) if e["level"] == "error"]

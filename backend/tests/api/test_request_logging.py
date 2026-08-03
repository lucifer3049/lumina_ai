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
from fastapi import FastAPI

from api.main import REQUEST_ID_HEADER, create_app
from config.logging import configure_logging
from core.exceptions import NotFoundError
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


@pytest.fixture
async def client(capsys: pytest.CaptureFixture[str]) -> AsyncIterator[httpx.AsyncClient]:
    """在 capsys 生效後才設定 logging——handler 綁的是當下的 sys.stdout。"""
    configure_logging(level="INFO", fmt="json")
    # 存取日誌的租戶綁定以 X-Tenant-Id 為輸入，需 spike 的 tenant_middleware。
    async with _client(create_app(enable_spike_endpoints=True)) as c:
        yield c


class TestAccessLog:
    async def test_every_request_emits_one_access_log(
        self, client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        response = await client.get("/api/v1/spike/does-not-exist")

        logs = _access_logs(capsys.readouterr().out)

        assert len(logs) == 1
        event = logs[0]
        assert event["method"] == "GET"
        assert event["path"] == "/api/v1/spike/does-not-exist"
        assert event["status"] == response.status_code
        assert isinstance(event["duration_ms"], (int, float))

    async def test_request_id_matches_response_header(
        self, client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """對外可見的 id 與 log 內的 id 必須是同一個，否則 client 回報的 id 撈不到東西。"""
        response = await client.get("/api/v1/spike/does-not-exist")

        assert (
            _access_logs(capsys.readouterr().out)[0]["request_id"]
            == (response.headers[REQUEST_ID_HEADER])
        )

    async def test_query_string_is_not_logged(
        self, client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        await client.get("/api/v1/spike/items?limit=20&token=super-secret")

        out = capsys.readouterr().out

        assert "super-secret" not in out
        assert _access_logs(out)[0]["path"] == "/api/v1/spike/items"

    async def test_tenant_id_is_bound_when_present(
        self, client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        await client.get(
            "/api/v1/spike/does-not-exist",
            headers={"X-Tenant-Id": str(TENANT_A)},
        )

        assert _access_logs(capsys.readouterr().out)[0]["tenant_id"] == str(TENANT_A)

    async def test_context_does_not_leak_between_requests(
        self, client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """第二個請求沒帶租戶，就不該看到第一個請求的 tenant_id。"""
        await client.get("/api/v1/spike/x", headers={"X-Tenant-Id": str(TENANT_A)})
        await client.get("/api/v1/spike/y")

        first, second = _access_logs(capsys.readouterr().out)

        assert first["request_id"] != second["request_id"]
        assert second.get("tenant_id") is None


class TestErrorCorrelation:
    """錯誤 log 與回應必須共用同一個 request_id（api/main.py 的 500 契約）。"""

    async def test_unhandled_error_log_shares_request_id(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="INFO", fmt="json")
        # 顯式關閉：本測試只用自掛的 /boom，不需要 spike 面。裸 create_app() 會改讀
        # AppSettings，測試結果就隨本機 .env 而變（跑壓測時 .env 可能是開的）。
        app = create_app(enable_spike_endpoints=False)

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

    async def test_domain_error_4xx_is_not_error_level(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """4xx 是使用者可修正的錯誤，不該進 ERROR（12 §1.1 等級紀律：ERROR = 需人看）。

        把 404 記成 ERROR 會讓告警噪音淹掉真正需要人看的事件。
        """
        configure_logging(level="INFO", fmt="json")
        app = create_app(enable_spike_endpoints=False)  # 理由同上：只用自掛的 /missing

        @app.get("/missing")
        async def missing() -> None:
            raise NotFoundError("找不到文件")

        async with _client(app) as client:
            await client.get("/missing")

        assert not [e for e in _log_lines(capsys.readouterr().out) if e["level"] == "error"]

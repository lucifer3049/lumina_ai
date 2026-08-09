"""驗收測試 —— 1A-5 spike 面移除（ADR-002 結案條件）。

ADR-002 的結案條件是四個名字在**同一個 commit** 消失：`tenant_middleware`
（api/main.py 的 spike 租戶綁定）、`api/v1/spike.py`、`apps/spike/`、
`ENABLE_SPIKE_ENDPOINTS` 旗標本身。這裡逐一以「不存在」釘住。

為什麼刪除也要驗收測試：刪除類變更最常見的殘留是「旗標刪了、行為還在」——
例如 `X-Tenant-Id` 的解析碼留在 middleware 裡只是再也沒人開啟。那個殘留沒有
任何症狀，直到某次重構把它重新接上。以下斷言讓每一種殘留都是紅燈：

1. 模組層：五個 spike 模組 import 不到（`api.schemas.spike` 與
   `services/repositories` 雖不在 ADR 的四個名字裡，但它們只被 spike 路由使用，
   留下即死碼——鐵則之外靠本測試釘住）。
2. 設定層：`AppSettings` 沒有旗標欄位、`create_app` 沒有參數、環境變數設了
   也完全惰性。
3. 行為層：client 自報的 `X-Tenant-Id` 不進 TenantContext、格式錯誤也不再
   產生 spike 專屬的 400（INVALID_TENANT_ID 不屬於 09 附錄 A 契約）。

OpenAPI 契約無 spike 路徑由 tests/unit/test_openapi_export.py 既有測試涵蓋，
此處不重複。
"""

from __future__ import annotations

import importlib.util
import inspect
import uuid
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI

from api.main import create_app
from config.settings.app_settings import AppSettings, get_app_settings

TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")

# ADR-002 點名的兩個 + 只為 spike 路由存在的三個（刪路由不刪它們 = 死碼）。
SPIKE_MODULES = [
    "apps.spike",
    "api.v1.spike",
    "api.schemas.spike",
    "services.spike",
    "repositories.spike",
]


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


@pytest.fixture
def spike_env_true(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """設 ``ENABLE_SPIKE_ENDPOINTS=true`` 並前後清 settings 快取。

    移除後這個環境變數應**完全惰性**——部署環境裡殘留的舊變數（.env、CI secret、
    compose 檔）不該有任何效果。不清快取的話本測試讀到的是別條測試建好的
    settings，等於沒設值（假綠燈）。
    """
    monkeypatch.setenv("ENABLE_SPIKE_ENDPOINTS", "true")
    get_app_settings.cache_clear()
    yield
    get_app_settings.cache_clear()


class TestSpikeCodeIsGone:
    """模組與設定層：名字本身必須消失。"""

    @pytest.mark.parametrize("module", SPIKE_MODULES)
    def test_spike_modules_do_not_exist(self, module: str) -> None:
        assert importlib.util.find_spec(module) is None, f"{module} 應隨 1A-5 刪除"

    def test_spike_app_not_installed(self) -> None:
        from django.conf import settings as django_settings

        leftovers = [app for app in django_settings.INSTALLED_APPS if "spike" in app]
        assert not leftovers, f"INSTALLED_APPS 殘留 {leftovers}"

    def test_app_settings_has_no_spike_flag(self) -> None:
        assert "enable_spike_endpoints" not in AppSettings.model_fields

    def test_create_app_takes_no_spike_parameter(self) -> None:
        """參數也要消失——留著一個永遠是 False 的參數，呼叫端會繼續傳它，
        測試裡到處都是 ``enable_spike_endpoints=False`` 的殭屍實參。"""
        assert "enable_spike_endpoints" not in inspect.signature(create_app).parameters


class TestSpikeSurfaceIsUnmountable:
    """行為層：即使環境變數殘留，spike 面也掛不起來。"""

    def test_env_flag_is_inert(self, spike_env_true: None) -> None:
        """反向殘留測試：env 設 true，app 仍然沒有任何 spike 路徑。

        這是刪除版的 ``test_env_flag_is_actually_read``（test_api_errors.py）——
        那條驗「旗標讀得到」，本條驗「旗標讀不到了」。用 OpenAPI ``paths`` 比對的
        理由同那條：include_router 的包裝物件沒有 ``path`` 屬性。
        """
        paths = create_app().openapi()["paths"]
        assert not [p for p in paths if "spike" in p]

    async def test_client_tenant_header_never_binds_tenant(self, spike_env_true: None) -> None:
        """client 自報的 ``X-Tenant-Id`` 不進 TenantContext——不論環境變數為何。

        與 test_api_errors.py 的 ``test_client_tenant_header_not_bound_when_disabled``
        同型，差別是那條靠「旗標關閉」成立，本條在旗標**已不存在**且環境變數殘留
        的情況下仍須成立：解析標頭的那段碼必須是刪掉了，不是關掉了。
        """
        from core.tenant import try_get_current_tenant_id

        app = create_app()

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
        assert response.json()["tenant_id"] is None

    async def test_malformed_tenant_header_is_ignored(self, spike_env_true: None) -> None:
        """格式錯誤的 ``X-Tenant-Id`` 不再產生 400。

        spike 期間 middleware 會對非 UUID 的標頭短路回 400（INVALID_TENANT_ID，
        刻意不入 09 附錄 A 的契約字典）。刪除後標頭是任意雜訊，請求照常路由——
        這裡打不存在的路徑，拿到的必須是一般的 404 而非 400：若還是 400，代表
        解析碼還活著。
        """
        async with _client(create_app()) as client:
            response = await client.get(
                "/api/v1/does-not-exist",
                headers={"X-Tenant-Id": "not-a-uuid"},
            )

        assert response.status_code == 404

"""驗收：存活與就緒探測（11 §3.2；二次架構審計 F-01）。

第一輪審計的 F-01 有三個部分，這是其中一個：**全 api/v1 沒有任何 healthz**，
所以編排器沒有東西可以問「這個容器好了沒」。

兩支端點的分工不是風格問題，是**故障放大**的問題：

- liveness 失敗 → 編排器**重啟容器**。所以它不能碰 DB——一次資料庫抖動會讓每一個
  健康的 API 容器被輪流殺掉，把一個可恢復的故障變成全面停機。
- readiness 失敗 → **從負載平衡摘掉**，不重啟。節點會在依賴恢復後自己回來。

三件事錯了都不會有例外，而且要等到部署當天才看得出來：

1. **需要認證**。存活探測打不到，容器永遠是 unhealthy。
2. **回應洩漏內部拓撲**（主機名、埠、連線字串）。探測端點無認證，等於公開它們。
3. **兩支合成一支**（見上）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from api.main import create_app

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


class TestLiveness:
    async def test_it_answers_without_credentials(self, client: httpx.AsyncClient) -> None:
        """無認證是必要條件，不是疏漏：探測要能在沒有憑證的情況下打。"""
        response = await client.get("/healthz")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_it_reports_generations_in_flight(self, client: httpx.AsyncClient) -> None:
        """關機演練時，這是「drain 有沒有真的在等」唯一看得到的數字。"""
        body = await client.get("/healthz")

        assert body.json()["generations_in_flight"] == 0

    async def test_it_leaks_no_topology(self, client: httpx.AsyncClient) -> None:
        """鐵則 9。無認證的端點回報主機名／埠／連線字串，等於公開基礎設施位置。

        比對的是**值**而不是欄位名：`orm_runtime_knobs()` 的兩個值是刻意選過的
        診斷數字（threadpool 寬度、連線壽命），而任何看起來像位址的東西都不該在。
        """
        body: dict[str, Any] = (await client.get("/healthz")).json()

        rendered = repr(body)
        for leak in ("127.0.0.1", "localhost", "postgres", "pgbouncer", "redis", "minio", "5432"):
            assert leak not in rendered, f"回應帶出了內部拓撲：{leak}"


class TestReadiness:
    async def test_it_reports_each_dependency(self, client: httpx.AsyncClient) -> None:
        """DB 與 Redis 都可達時回 200，且逐項說明——維運要知道是哪一個掛了。"""
        response = await client.get("/readyz")

        assert response.status_code == 200
        assert response.json()["checks"] == {"database": True, "redis": True}

    async def test_a_dead_dependency_is_a_503(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**503 而不是 500**：編排器只看狀態碼，而 5xx 走 `DomainError` 那條路會
        把一次依賴故障寫成 ERROR 級日誌——readiness 每幾秒打一次，依賴掛掉的那幾
        分鐘會產生幾百筆 ERROR，把真正需要人看的事件淹掉（12 §1.1）。
        """
        monkeypatch.setattr("api.health.probe_redis", lambda: False)

        response = await client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["checks"]["redis"] is False

    async def test_a_failure_does_not_explain_itself_to_the_caller(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """失敗原因寫進 log（那裡有 request_id），不回給呼叫端——它無認證。"""
        monkeypatch.setattr("api.health.probe_database", lambda: False)

        rendered = repr((await client.get("/readyz")).json())

        for leak in ("127.0.0.1", "postgres", "pgbouncer", "password", "5432"):
            assert leak not in rendered


class TestTheyAreNotBusinessApi:
    async def test_they_are_not_under_the_versioned_prefix(self, client: httpx.AsyncClient) -> None:
        """探測路徑寫在部署設定裡。跟著 API 版本走的話，每次改版都要同步改一份
        部署設定，而漏改的症狀是「容器一直被判定不健康」。"""
        assert (await client.get("/api/v1/healthz")).status_code == 404

    async def test_they_stay_out_of_the_openapi_contract(self, client: httpx.AsyncClient) -> None:
        """前端的 codegen 讀這份契約（鐵則 10）。探測端點進去只會產生兩個沒有人
        會呼叫的 client 方法，而它們每次都要跟著契約檢查一起 review。"""
        paths = (await client.get("/openapi.json")).json()["paths"]

        assert "/healthz" not in paths
        assert "/readyz" not in paths

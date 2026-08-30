"""驗收：HTTP 頻率限制（09 §1.3、10 §2.1；二次架構審計 F-11＋L3）。

**與配額是兩件事**：配額問「這個租戶這一期還有多少額度」（要先認證才知道租戶是誰），
這一層問「這個來源這一分鐘打了幾次」——而它必須在認證**之前**生效，否則登入端點
完全沒有保護。

**L3 是第二個理由**：`AuthService` 的登入失敗計數以 `tenant+email` 為鍵、每次失敗
重設 TTL，所以知道租戶 slug 與 email 的人可以持續鎖住任何帳號（那個 docstring 自己
寫著「持續攻擊會讓鎖定持續延長」）。per-IP 的擋線把「無限次嘗試」變成「每分鐘 N 次」。

四件事錯了都不會有錯誤訊息：

1. **認證端點與一般端點共用一個桶**。壓到跟登入一樣嚴，正常使用者開一個聊天頁就會
   被擋；放到跟一般端點一樣寬，暴力破解每分鐘可以猜 300 次。
2. **採信 `X-Forwarded-For`**。那是 client 送的標頭，每個請求換一個假 IP，限流就
   **安靜地**失效——計數器照樣在動，只是每個 key 都只有 1。
3. **探測端點被算進去**。編排器每幾秒打一次 `/readyz`，把它算進桶裡等於讓 probe
   自己把節點打成 429，而那看起來像應用壞了。
4. **Redis 掛掉時擋下全部請求**。限流是保護機制不是安全邊界，fail closed 等於用一個
   確定的故障換一個可能的攻擊。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from api.main import create_app
from config.settings.app_settings import get_app_settings
from core.redis import get_redis

pytestmark = pytest.mark.django_db(transaction=True)

_LOGIN = "/api/v1/auth/login"
_CREDENTIALS = {"tenant_slug": "nope", "email": "nobody@example.com", "password": "wrong"}


@pytest.fixture(autouse=True)
def _clean_counters() -> Iterator[None]:
    """限流的 key 不帶租戶前綴（這一層跑在認證之前，租戶還不知道是誰），
    所以要自己收——否則同一分鐘內的下一條測試會繼承上一條的計數。"""
    yield
    client = get_redis()
    keys = list(client.scan_iter(match="rl:*"))
    if keys:
        client.delete(*keys)


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把限流打開並壓到很小的額度。

    測試環境預設關掉它（`tests/conftest.py`，理由見該處）——要驗它的測試自己打開，
    那也讓「這條測試在驗限流」在檔案裡看得見。
    """
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_PER_MINUTE", "3")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "5")
    get_app_settings.cache_clear()
    yield
    get_app_settings.cache_clear()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


async def _login(client: httpx.AsyncClient, **headers: str) -> httpx.Response:
    return await client.post(_LOGIN, json=_CREDENTIALS, headers=headers or None)


class TestAuthBucket:
    async def test_it_refuses_after_the_allowance(
        self, client: httpx.AsyncClient, limits: None
    ) -> None:
        """**L3 的正題**：無限次的密碼嘗試變成每分鐘 N 次。

        前 3 次是 401（帳號不存在，那是正常的認證失敗）；第 4 次連認證都不會跑到。
        """
        statuses = [(await _login(client)).status_code for _ in range(4)]

        assert statuses[:3] == [401, 401, 401], statuses
        assert statuses[3] == 429

    async def test_the_refusal_carries_a_retry_hint(
        self, client: httpx.AsyncClient, limits: None
    ) -> None:
        """429 不附重試時間等於叫 client 自己猜，而猜出來的多半是「立刻重試」
        ——正好在最不該加壓的時候加壓。"""
        for _ in range(3):
            await _login(client)

        refused = await _login(client)

        assert refused.status_code == 429
        assert refused.headers["Retry-After"].isdigit()
        body = refused.json()
        assert body["code"] == "RATE_LIMITED"
        assert body["details"]["retry_after_seconds"] > 0

    async def test_a_refusal_still_gets_a_request_id(
        self, client: httpx.AsyncClient, limits: None
    ) -> None:
        """限流掛在追蹤 context **內**（`api/main.py` 的掛載順序）。429 若在日誌上
        完全看不見，「使用者說一直被擋」就查不出是哪一道擋的。"""
        for _ in range(3):
            await _login(client)

        refused = await _login(client)

        assert refused.headers["X-Request-Id"]
        assert refused.json()["request_id"] == refused.headers["X-Request-Id"]


class TestBucketsAreSeparate:
    async def test_the_auth_allowance_does_not_drain_the_general_one(
        self, client: httpx.AsyncClient, limits: None
    ) -> None:
        """共用一個桶的話：壓到跟登入一樣嚴，正常使用者開一個聊天頁就被擋；
        放到跟一般端點一樣寬，暴力破解每分鐘可以猜 300 次。"""
        for _ in range(4):
            await _login(client)

        # 認證桶已滿，一般桶還沒被動過——一個未認證的一般端點該回 401 而不是 429。
        other = await client.get("/api/v1/conversations")

        assert other.status_code != 429, "認證桶的用量漏到一般桶了"


class TestExemptions:
    async def test_probes_are_never_rate_limited(
        self, client: httpx.AsyncClient, limits: None
    ) -> None:
        """編排器每幾秒打一次。算進桶裡等於讓 probe 自己把節點打成 429，
        而 K8s 對 readiness 失敗的處置是把節點從 LB 上摘掉——全部節點。"""
        statuses = [(await client.get("/readyz")).status_code for _ in range(10)]

        assert 429 not in statuses


class TestProxyHeaders:
    async def test_forwarded_headers_are_ignored_by_default(
        self, client: httpx.AsyncClient, limits: None
    ) -> None:
        """**這是安全相關的預設。** 採信 `X-Forwarded-For` 時，每個請求換一個假 IP
        就能繞過限流，而且它會**安靜地**失效：計數器照樣在動，只是每個 key 都是 1。

        這裡每次換一個假來源，第 4 次仍然要被擋——證明用的是 socket 的來源位址。
        """
        statuses = [
            (await _login(client, **{"X-Forwarded-For": f"10.0.0.{i}"})).status_code
            for i in range(4)
        ]

        assert statuses[3] == 429, "X-Forwarded-For 被採信了，限流可被任意繞過"

    async def test_it_can_be_opted_into(
        self, client: httpx.AsyncClient, limits: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """反向代理落地之後（Phase 4）需要它——否則所有請求都算在代理那一個 IP 上，
        一個使用者就能把全部人擋掉。"""
        monkeypatch.setenv("RATE_LIMIT_TRUST_PROXY_HEADERS", "true")
        get_app_settings.cache_clear()

        statuses = [
            (await _login(client, **{"X-Forwarded-For": f"10.0.0.{i}"})).status_code
            for i in range(4)
        ]

        assert 429 not in statuses, "開了之後每個來源該有自己的桶"


class TestDegradation:
    async def test_a_dead_backend_lets_traffic_through(
        self, client: httpx.AsyncClient, limits: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**fail open，與系統其他地方相反，這是刻意的。**

        限流是保護機制不是安全邊界（認證、租戶隔離、配額都不在這一層）。讓它在
        Redis 抖動時把整個網站關掉，是用一個確定的故障換一個可能的攻擊。
        """

        def explode() -> None:
            raise RuntimeError("redis is down")

        monkeypatch.setattr("api.middleware.rate_limit.get_async_redis", explode)

        statuses = [(await _login(client)).status_code for _ in range(5)]

        assert 429 not in statuses


class TestItStaysOffTheEventLoop:
    """middleware 跑在 event loop 上，沒有 threadpool 可躲——同步 client 的每一次
    incr 都是 loop 上的阻塞 I/O。Redis 一抖（failover、網路抖動），每個請求都掛到
    socket timeout，這個 replica 的所有 in-flight 請求與 SSE 串流被串行化——症狀
    是「Redis 一抖整站凍結」（2026-08-30 深度審查）。"""

    def test_the_counter_is_awaited_not_blocking(self) -> None:
        import inspect

        from api.middleware.rate_limit import RateLimitMiddleware

        assert inspect.iscoroutinefunction(RateLimitMiddleware._within), (
            "_within 必須是 coroutine——同步版本會在 event loop 上做阻塞 Redis I/O"
        )

    def test_the_module_does_not_touch_the_sync_client(self) -> None:
        import inspect
        from pathlib import Path

        from api.middleware import rate_limit

        source = Path(inspect.getfile(rate_limit)).read_text(encoding="utf-8")

        assert "get_async_redis" in source
        assert "from core.redis import get_redis" not in source, (
            "限流改回同步 client 了——那是 event loop 上的阻塞 I/O"
        )


class TestTheSwitch:
    async def test_disabled_means_no_counting(self, client: httpx.AsyncClient) -> None:
        """預設在測試環境是關的（conftest）。關掉時連 Redis 都不該碰。"""
        statuses = [(await _login(client)).status_code for _ in range(6)]

        assert 429 not in statuses
        assert not list(get_redis().scan_iter(match="rl:*"))

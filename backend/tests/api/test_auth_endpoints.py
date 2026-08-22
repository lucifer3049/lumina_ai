"""驗收：`/auth` 端點（09 §2.1、10 §2.1）。

這是整個系統第一個「真的有人使用」的端點，也是租戶身分的唯一合法來源——
1A-2 建好的 RLS 一直在等這個值。

四組斷言，每一組都對應一種真實的攻擊或事故：

1. **登入成功的形狀**：access token 在 body、refresh 在 httpOnly cookie。
   refresh 若放 body，前端就得自己存（localStorage 是 XSS 的直接目標）；
   httpOnly 讓 JavaScript 讀不到它。
2. **失敗回應不可區分**：帳號不存在與密碼錯誤必須回一模一樣的東西。有差別的話
   攻擊者可以拿一份 email 名單反覆試，先篩出「這家公司有哪些人」再集中攻擊。
3. **鎖定**：連續失敗 5 次鎖 15 分鐘，而且**鎖定期間即使密碼正確也不放行**——
   否則暴力破解只是變慢，沒有被擋住。
4. **登出真的讓 token 失效**：JWT 是自我驗證的，伺服器預設「不知道」誰登出過。
   沒有撤銷名單的話，登出只是前端把 token 丟掉，被竊取的那份照樣能用 15 分鐘。

租戶來源是「已驗證的 JWT claim」，而且是唯一來源（ADR-002）。因此本檔也驗
``X-Tenant-Id`` 這類 client 自報的標頭**完全不影響**身分——1A-5 已把讀取那個
標頭的程式碼整段刪除，這裡從**行為面**確認：帶著它打正式端點，身分不會改變。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI

from api.dependencies.auth import Principal, require_authenticated
from api.main import create_app
from common.passwords import hash_password
from config.settings.app_settings import get_app_settings
from core.redis import get_redis, tenant_key
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, make_user, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "correct horse battery staple"
EMAIL = "person@example.com"
# 登入必須指名租戶：RLS 之下，「還沒有租戶身分」的查詢一筆都看不到，而 email
# 只在租戶內唯一。slug → tenant_id 由不含 RLS 的目錄表解析（09 §2.1，1A-3 補述）。
SLUG_A = "tenant-a"
SLUG_B = "tenant-b"

# 10 §2.1：失敗 5 次 → 鎖 15 分鐘
MAX_FAILED_ATTEMPTS = 5


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    """清掉本測試租戶的 Redis 狀態（鎖定計數、撤銷名單、refresh 家族）。

    只刪這兩個租戶的前綴而不是 flushdb：開發機的 Redis 是共用的，flush 會清掉
    別人正在跑的東西，而那種失敗極難重現。
    """
    yield
    client = get_redis()
    for tenant_id in (TENANT_A, TENANT_B):
        keys = list(client.scan_iter(match=tenant_key(tenant_id, "*")))
        if keys:
            client.delete(*keys)


@pytest.fixture
def user_in_tenant_b() -> uuid.UUID:
    """租戶 B 的同名信箱帳號，密碼不同。

    fixture 而非測試內建立：ORM 是同步的，在 async 測試函式裡直接呼叫會被 Django
    擋下（``SynchronousOnlyOperation``）。fixture 是同步的，跑在 event loop 之外。
    """
    with tenant_scope(TENANT_B):
        make_tenant(id=TENANT_B, slug=SLUG_B)
        user = make_user(
            tenant_id=TENANT_B, email=EMAIL, password_hash=hash_password("other-secret")
        )
    return user.id


@pytest.fixture
def user_in_tenant_a() -> uuid.UUID:
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a")
        user = make_user(tenant_id=TENANT_A, email=EMAIL, password_hash=hash_password(PASSWORD))
    return user.id


def _app() -> FastAPI:
    app = create_app()

    @app.get("/probe")
    async def probe(
        principal: Annotated[Principal, Depends(require_authenticated)],
    ) -> dict[str, str]:
        """測試專用的受保護端點。

        用自掛的 route 而不是等 1A-4 的正式端點：認證與授權要能分開驗收，
        否則這張卡的紅綠燈會跟權限判定綁在一起。
        """
        return {"user_id": str(principal.user_id), "tenant_id": str(principal.tenant_id)}

    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


async def _login(
    client: httpx.AsyncClient,
    *,
    email: str = EMAIL,
    password: str = PASSWORD,
    tenant_slug: str = SLUG_A,
) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )


# ── 登入成功 ────────────────────────────────────────────────────


class TestLoginSuccess:
    async def test_returns_access_token_and_sets_refresh_cookie(
        self, client: httpx.AsyncClient, user_in_tenant_a: uuid.UUID
    ) -> None:
        response = await _login(client)

        assert response.status_code == 200
        assert response.json()["access_token"]
        assert response.json()["token_type"] == "Bearer"
        assert "refresh_token" in response.cookies, "refresh token 必須走 cookie，不放 body"
        assert "refresh_token" not in response.text, "refresh token 不可同時出現在 body"

    async def test_refresh_cookie_is_http_only(
        self, client: httpx.AsyncClient, user_in_tenant_a: uuid.UUID
    ) -> None:
        """httpOnly 讓 JavaScript 讀不到 refresh token——XSS 的主要止血點。"""
        response = await _login(client)

        set_cookie = response.headers["set-cookie"]

        assert "httponly" in set_cookie.lower()
        assert "samesite=lax" in set_cookie.lower().replace(" ", "")

    async def test_refresh_cookie_is_secure_outside_development(
        self,
        client: httpx.AsyncClient,
        user_in_tenant_a: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`Secure` 只在開發環境關掉，其他環境一律開。

        為什麼要關：`Secure` 的意思是「只在 HTTPS 連線送出」，而本機開發跑的是
        ``http://localhost``——瀏覽器會直接丟掉這個 cookie，症狀是「登入成功、
        一重新整理就登出」，而 devtools 裡根本看不到那個 cookie。

        為什麼要有這條測試：這種「為了本機方便而放寬的設定」最容易跟著上正式
        環境，而在正式環境少了 `Secure`，任何一次降級到 http 的請求都會把 refresh
        token 明文送上網路。所以放寬的條件必須被釘死在測試裡。
        """
        monkeypatch.setenv("ENVIRONMENT", "production")
        get_app_settings.cache_clear()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(), raise_app_exceptions=False),
            base_url="http://testserver",
        ) as production_client:
            response = await _login(production_client)

        get_app_settings.cache_clear()

        assert "secure" in response.headers["set-cookie"].lower()

    async def test_access_token_grants_access_to_a_protected_route(
        self, client: httpx.AsyncClient, user_in_tenant_a: uuid.UUID
    ) -> None:
        token = (await _login(client)).json()["access_token"]

        response = await client.get("/probe", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200
        assert response.json()["tenant_id"] == str(TENANT_A)
        assert response.json()["user_id"] == str(user_in_tenant_a)


# ── 失敗與不可區分性 ────────────────────────────────────────────


class TestLoginFailure:
    async def test_wrong_password_returns_401(
        self, client: httpx.AsyncClient, user_in_tenant_a: uuid.UUID
    ) -> None:
        response = await _login(client, password="wrong")

        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_INVALID_CREDENTIALS"

    async def test_unknown_account_is_indistinguishable_from_a_wrong_password(
        self, client: httpx.AsyncClient, user_in_tenant_a: uuid.UUID
    ) -> None:
        """兩種失敗的回應必須逐字相同（只有 request_id 會不一樣）。

        有差別的話，攻擊者能拿一份 email 名單反覆試，先確定「這家公司有哪些人」
        ——那份名單本身就是有價值的情報，也讓後續攻擊更集中。
        """
        wrong_password = (await _login(client, password="wrong")).json()
        unknown_user = (await _login(client, email="nobody@example.com")).json()

        wrong_password.pop("request_id", None)
        unknown_user.pop("request_id", None)

        assert wrong_password == unknown_user

    async def test_credentials_of_another_tenant_do_not_work_on_this_one(
        self,
        client: httpx.AsyncClient,
        user_in_tenant_a: uuid.UUID,
        user_in_tenant_b: uuid.UUID,
    ) -> None:
        """同 email 在兩個租戶各有帳號時，密碼各自獨立。

        email 只在租戶內唯一（1A-2）。若登入查詢沒有租戶維度，租戶 B 的使用者
        會用自己的密碼登入到租戶 A 的帳號——跨租戶帳號接管，而且沒有錯誤訊息。
        """
        assert (await _login(client, password="other-secret")).status_code == 401
        assert (
            await _login(client, tenant_slug=SLUG_B, password="other-secret")
        ).status_code == 200

    async def test_unknown_tenant_slug_looks_exactly_like_a_wrong_password(
        self, client: httpx.AsyncClient, user_in_tenant_a: uuid.UUID
    ) -> None:
        """slug 不存在也回 401、內容與密碼錯誤相同。

        有差別的話，這個端點就變成「查詢平台有哪些客戶」的工具——競爭對手可以
        拿公司名稱清單掃一遍，而那份客戶名單本身就是商業情報。
        """
        wrong_password = (await _login(client, password="wrong")).json()
        unknown_tenant = (await _login(client, tenant_slug="no-such-company")).json()

        wrong_password.pop("request_id", None)
        unknown_tenant.pop("request_id", None)

        assert wrong_password == unknown_tenant


class TestAccountLockout:
    async def test_locks_after_five_failures(
        self, client: httpx.AsyncClient, user_in_tenant_a: uuid.UUID
    ) -> None:
        for _ in range(MAX_FAILED_ATTEMPTS):
            await _login(client, password="wrong")

        response = await _login(client, password="wrong")

        assert response.status_code == 423
        assert response.json()["code"] == "ACCOUNT_LOCKED"

    async def test_correct_password_is_still_refused_while_locked(
        self, client: httpx.AsyncClient, user_in_tenant_a: uuid.UUID
    ) -> None:
        """鎖定期間連正確密碼都不放行——否則暴力破解只是變慢，並沒有被擋住。"""
        for _ in range(MAX_FAILED_ATTEMPTS):
            await _login(client, password="wrong")

        response = await _login(client)

        assert response.status_code == 423

    async def test_successful_login_resets_the_failure_counter(
        self, client: httpx.AsyncClient, user_in_tenant_a: uuid.UUID
    ) -> None:
        """成功登入要清掉計數，否則偶發的打錯字會日積月累把人鎖死。"""
        for _ in range(MAX_FAILED_ATTEMPTS - 1):
            await _login(client, password="wrong")

        assert (await _login(client)).status_code == 200

        for _ in range(MAX_FAILED_ATTEMPTS - 1):
            await _login(client, password="wrong")

        assert (await _login(client)).status_code == 200


# ── 登出與撤銷 ──────────────────────────────────────────────────


class TestLogout:
    async def test_access_token_stops_working_after_logout(
        self, client: httpx.AsyncClient, user_in_tenant_a: uuid.UUID
    ) -> None:
        """JWT 是自我驗證的，伺服器預設不知道誰登出過。

        沒有 jti 撤銷名單的話，「登出」只是前端把 token 丟掉——被竊取的那一份
        照樣能用滿 15 分鐘，而使用者以為自己已經登出了。
        """
        token = (await _login(client)).json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        assert (await client.get("/probe", headers=auth)).status_code == 200
        assert (await client.post("/api/v1/auth/logout", headers=auth)).status_code == 204

        response = await client.get("/probe", headers=auth)

        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_TOKEN_REVOKED"


# ── 租戶來源（ADR-002）──────────────────────────────────────────


class TestTenantSource:
    async def test_tenant_comes_from_the_token_not_from_a_client_header(
        self, client: httpx.AsyncClient, user_in_tenant_a: uuid.UUID
    ) -> None:
        """client 自報的租戶標頭一律無效（鐵則 4）。

        ``X-Tenant-Id`` 曾經是 spike 期的租戶來源（1A-5 已刪）。這條測試留著的
        理由不是那段歷史，而是**任何** client 送來的租戶宣稱都不該有效：日後若有人
        「為了方便」重新接一條標頭取租戶的捷徑，這裡會紅。
        """
        token = (await _login(client)).json()["access_token"]

        response = await client.get(
            "/probe",
            headers={"Authorization": f"Bearer {token}", "X-Tenant-Id": str(TENANT_B)},
        )

        assert response.json()["tenant_id"] == str(TENANT_A)

    async def test_missing_token_is_401(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/probe")

        assert response.status_code == 401


class TestChangePasswordThrottling:
    """改密碼的「驗證舊密碼」與登入共用同一把鎖（10 §2.1）。

    **只擋登入是擋不住的**：`POST /auth/password/change` 也拿明文密碼去比對，而它
    只需要一張 access token。偷到 token 的人（XSS、側錄）可以在它 15 分鐘的壽命裡
    對這個端點全速猜舊密碼，一次都不會被計數——猜中就是接管帳號，因為改密碼會順便
    撤銷原主的所有 session。

    共用計數器之後，那條路徑的失敗與登入的失敗會累加，鎖住之後兩邊都進不來。
    """

    async def _change(
        self, client: httpx.AsyncClient, token: str, *, current: str
    ) -> httpx.Response:
        return await client.post(
            "/api/v1/auth/password/change",
            headers={"Authorization": f"Bearer {token}"},
            json={"current_password": current, "new_password": "a brand new passphrase"},
        )

    async def test_guessing_the_old_password_is_not_unlimited(
        self, client: httpx.AsyncClient, user_in_tenant_a: uuid.UUID
    ) -> None:
        token = (await _login(client)).json()["access_token"]

        for _ in range(MAX_FAILED_ATTEMPTS):
            assert (await self._change(client, token, current="wrong")).status_code == 401

        response = await self._change(client, token, current="wrong")

        assert response.status_code == 423
        assert response.json()["code"] == "ACCOUNT_LOCKED"

    async def test_those_failures_also_lock_the_login(
        self, client: httpx.AsyncClient, user_in_tenant_a: uuid.UUID
    ) -> None:
        """兩條路徑共用一個計數器——分開算的話，攻擊者只要換一條路就沒有上限。"""
        token = (await _login(client)).json()["access_token"]

        for _ in range(MAX_FAILED_ATTEMPTS):
            await self._change(client, token, current="wrong")

        assert (await _login(client)).status_code == 423

    async def test_a_successful_change_clears_the_counter(
        self, client: httpx.AsyncClient, user_in_tenant_a: uuid.UUID
    ) -> None:
        """打錯幾次之後改成功了，計數要歸零——否則偶發的打錯字會日積月累把人鎖死。"""
        token = (await _login(client)).json()["access_token"]

        for _ in range(MAX_FAILED_ATTEMPTS - 1):
            await self._change(client, token, current="wrong")

        assert (await self._change(client, token, current=PASSWORD)).status_code == 204

        # 新密碼登入照常（改密碼會撤銷所有 session，所以這裡本來就要重登）。
        assert (await _login(client, password="a brand new passphrase")).status_code == 200

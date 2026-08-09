"""驗收：refresh token rotation 與竊取偵測（10 §2.1）。

**rotation 是什麼**：每次拿 refresh token 換新的一組時，舊的那張立刻作廢、發一張
全新的。好處是被竊取的 refresh token 只在「下一次正常換發之前」有效。

**竊取偵測靠的是「已用過的 refresh 被再次使用」這個訊號**：正常流程下同一張
refresh 只會被用一次。若一張已經用過的又出現，代表有兩個持有者——本人與攻擊者，
其中一個用了、另一個接著用。系統無法分辨哪一個是本人，所以**整個家族一起撤銷**，
逼真正的使用者重新登入。

這個取捨要講清楚：撤銷全家族會讓本人也被登出，體驗上是負面的。但另一個選項是
「讓兩者都繼續有效」，那等於默認攻擊者長期在線。寧可讓本人重新輸一次密碼。

**family（家族）**：一次登入建立一個家族 id，之後每次 rotation 都沿用它。所以
「撤銷家族」等於「撤銷這台裝置的這次登入」，不影響使用者在其他裝置的 session
（多裝置管理的完整功能在 10 §2.3，排 Phase 3D；家族鍵現在就要有，否則之後補
等於所有既有 session 都失效）。

本檔走 service 層而非 HTTP：rotation 的狀態機在 `AuthService`，用 HTTP 驗會混入
cookie 與端點的細節，失敗時分不清是哪一層的問題。端點那一層由
`tests/api/test_auth_endpoints.py` 覆蓋。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from common.passwords import hash_password
from core.redis import get_redis, tenant_key
from services.identity.auth import AuthService

from core.exceptions import TokenRevokedError
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, make_user, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "correct horse battery staple"
EMAIL = "person@example.com"


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    keys = list(client.scan_iter(match=tenant_key(TENANT_A, "*")))
    if keys:
        client.delete(*keys)


@pytest.fixture
def user_id() -> uuid.UUID:
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a")
        user = make_user(tenant_id=TENANT_A, email=EMAIL, password_hash=hash_password(PASSWORD))
    return user.id


@pytest.fixture
def auth() -> AuthService:
    return AuthService()


def test_rotation_issues_a_new_pair(auth: AuthService, user_id: uuid.UUID) -> None:
    first = auth.login(tenant_id=TENANT_A, email=EMAIL, password=PASSWORD)
    second = auth.refresh(first.refresh_token)

    assert second.refresh_token != first.refresh_token
    assert second.access_token != first.access_token


def test_the_old_refresh_token_stops_working_immediately(
    auth: AuthService, user_id: uuid.UUID
) -> None:
    """換發之後舊的立刻作廢——這就是 rotation 的全部價值。

    少了它，一張外洩的 refresh token 可以用滿 14 天，而且每次都能換到新的 access
    token，等於永久存取權。
    """
    first = auth.login(tenant_id=TENANT_A, email=EMAIL, password=PASSWORD)
    auth.refresh(first.refresh_token)

    with pytest.raises(TokenRevokedError):
        auth.refresh(first.refresh_token)


def test_replaying_a_used_refresh_token_kills_the_whole_family(
    auth: AuthService, user_id: uuid.UUID
) -> None:
    """重放已用過的 refresh = 有兩個持有者 → 撤銷整個家族。

    這條是竊取偵測的核心。注意斷言的是**第二張（當前有效的）也一起失效**：
    只把重放的那張擋掉沒有意義，攻擊者手上本來就沒有最新的那張——真正該中止的
    是這條 session 鏈本身。
    """
    first = auth.login(tenant_id=TENANT_A, email=EMAIL, password=PASSWORD)
    second = auth.refresh(first.refresh_token)

    with pytest.raises(TokenRevokedError):
        auth.refresh(first.refresh_token)

    with pytest.raises(TokenRevokedError):
        auth.refresh(second.refresh_token)


def test_revoking_one_family_does_not_touch_another_login(
    auth: AuthService, user_id: uuid.UUID
) -> None:
    """兩次登入 = 兩個家族（想像成兩台裝置）。其中一台被判定竊取，另一台不受影響。

    少了家族的概念，竊取偵測只能「撤銷這個使用者的全部 session」——手機被盜用
    會把桌機也一起登出，而使用者完全不知道為什麼。
    """
    laptop = auth.login(tenant_id=TENANT_A, email=EMAIL, password=PASSWORD)
    phone = auth.login(tenant_id=TENANT_A, email=EMAIL, password=PASSWORD)

    auth.refresh(laptop.refresh_token)
    with pytest.raises(TokenRevokedError):
        auth.refresh(laptop.refresh_token)

    assert auth.refresh(phone.refresh_token) is not None


def test_token_version_bump_invalidates_every_family(auth: AuthService, user_id: uuid.UUID) -> None:
    """改密碼 / 停用帳號要讓**所有**裝置的 token 失效（10 §2.1）。

    這跟家族撤銷是不同層級的工具：家族處理「某一台裝置出事」，`token_version`
    處理「這個帳號本身出事」。後者不需要知道有哪些 jti 或家族存在，這正是它的
    價值——帳號被盜時，你根本不知道攻擊者建立了幾個 session。
    """
    session = auth.login(tenant_id=TENANT_A, email=EMAIL, password=PASSWORD)

    auth.bump_token_version(tenant_id=TENANT_A, user_id=user_id)

    with pytest.raises(TokenRevokedError):
        auth.refresh(session.refresh_token)


def test_refresh_keeps_the_tenant_of_the_original_login(
    auth: AuthService, user_id: uuid.UUID
) -> None:
    """換發出來的新 token 必須維持同一個租戶。

    租戶是從 refresh token 的 claim 來的，而換發時要重新簽一張——若那裡漏帶租戶，
    新的 access token 會沒有租戶或帶到錯的租戶，而 RLS 之下的症狀是「登入後
    突然什麼資料都看不到」，很難聯想到換發流程。
    """
    first = auth.login(tenant_id=TENANT_A, email=EMAIL, password=PASSWORD)
    second = auth.refresh(first.refresh_token)

    assert auth.describe_access_token(second.access_token).tenant_id == TENANT_A

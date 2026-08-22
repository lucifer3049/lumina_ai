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
from config.settings.app_settings import get_app_settings
from core.exceptions import TokenRevokedError
from core.redis import get_redis, tenant_key
from services.identity.auth import AuthService
from services.identity.tokens import get_token_codec
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


@pytest.fixture
def strict_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    """把寬限期關掉。

    驗「舊 token 作廢」與「重放撤銷家族」時必須關掉它——否則測到的是寬限期而不是
    輪換本身，而那兩件事的失效方式完全不同。設定物件是單例，`AuthService` 持有的
    就是同一個實例。
    """
    monkeypatch.setattr(get_app_settings(), "refresh_rotation_grace_seconds", 0)


def _expire_grace(refresh_token: str) -> None:
    """模擬寬限期到期：直接刪掉那個 key（TTL 只有幾秒，等它自然過期會讓測試變慢）。"""
    jti = get_token_codec().decode_refresh(refresh_token).jti
    get_redis().delete(tenant_key(TENANT_A, "refresh-rotated", str(jti)))


def test_rotation_issues_a_new_pair(auth: AuthService, user_id: uuid.UUID) -> None:
    first = auth.login(tenant_id=TENANT_A, email=EMAIL, password=PASSWORD)
    second = auth.refresh(first.refresh_token)

    assert second.refresh_token != first.refresh_token
    assert second.access_token != first.access_token


def test_the_old_refresh_token_stops_working_after_the_grace_window(
    auth: AuthService, user_id: uuid.UUID, strict_rotation: None
) -> None:
    """換發之後舊的就作廢——這就是 rotation 的全部價值。

    少了它，一張外洩的 refresh token 可以用滿 14 天，而且每次都能換到新的 access
    token，等於永久存取權。

    這裡把寬限期關掉（`strict_rotation`）才測得到「作廢」本身：預設有數秒窗口，
    而那個窗口是給**同一瞬間**的併發換發用的，見 `TestConcurrentRotation`。
    """
    first = auth.login(tenant_id=TENANT_A, email=EMAIL, password=PASSWORD)
    auth.refresh(first.refresh_token)

    with pytest.raises(TokenRevokedError):
        auth.refresh(first.refresh_token)


def test_replaying_a_used_refresh_token_kills_the_whole_family(
    auth: AuthService, user_id: uuid.UUID, strict_rotation: None
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
    _expire_grace(laptop.refresh_token)
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


class TestConcurrentRotation:
    """同一張 refresh 被兩個請求**同時**使用——多分頁同時喚醒、前端重試，都是常態。

    輪換原本是 `GET` 家族 → 比對 → `SET` 新值三步，之間沒有原子性，於是兩個方向
    的錯誤同時存在：

    - 兩邊都通過比對、都拿到新 token，家族值被後寫的那個蓋掉——**先寫那邊發出去的
      refresh 下次使用時會被判成重放，整個家族撤銷**。正常使用者被隨機登出，而且
      重現不了（要兩個請求撞在同一毫秒）。
    - 反過來，真正的攻擊者若恰好與本人同時換發，兩邊都會成功，偵測形同失效。

    處置是把比對與改寫下沉成一段 Lua，並讓輸掉競賽的那個請求拿到**贏家的那一張**
    token（寬限期）。本組測試釘住的是「兩個分頁最後握著同一張、而且都還活著」。
    """

    def test_both_callers_get_a_usable_token(self, auth: AuthService, user_id: uuid.UUID) -> None:
        first = auth.login(tenant_id=TENANT_A, email=EMAIL, password=PASSWORD)

        tab_one = auth.refresh(first.refresh_token)
        tab_two = auth.refresh(first.refresh_token)

        # 同一張——各發一張的話，家族只記得住一張，另一張下次使用就是「重放」。
        assert tab_two.refresh_token == tab_one.refresh_token
        # access token 各簽各的：它不進家族，兩個分頁本來就該各拿各的。
        assert tab_two.access_token != tab_one.access_token

    def test_the_family_survives(self, auth: AuthService, user_id: uuid.UUID) -> None:
        """**這條才是原本的災情。** 併發換發之後，下一次正常換發必須照常成功。

        舊寫法在這裡會 raise：兩個分頁各拿到一張，而家族只記得住後寫的那張，於是
        另一張一出現就被當成重放——使用者莫名其妙被登出，而 log 上寫的是「偵測到
        refresh token 重複使用」。
        """
        first = auth.login(tenant_id=TENANT_A, email=EMAIL, password=PASSWORD)
        tab_one = auth.refresh(first.refresh_token)
        tab_two = auth.refresh(first.refresh_token)

        assert auth.refresh(tab_one.refresh_token) is not None
        assert auth.describe_access_token(tab_two.access_token).tenant_id == TENANT_A

    def test_outside_the_window_it_is_still_a_replay(
        self, auth: AuthService, user_id: uuid.UUID
    ) -> None:
        """寬限期不是「重放偵測關掉了」：窗口一過，同一張 refresh 再出現照樣撤銷家族。

        攻擊者的重放來自別的時間點（偷到之後才用），本人的併發則差幾十毫秒——
        窗口取秒級就是為了把這兩件事分開。
        """
        first = auth.login(tenant_id=TENANT_A, email=EMAIL, password=PASSWORD)
        current = auth.refresh(first.refresh_token)
        _expire_grace(first.refresh_token)

        with pytest.raises(TokenRevokedError):
            auth.refresh(first.refresh_token)

        with pytest.raises(TokenRevokedError):
            auth.refresh(current.refresh_token)

    def test_logout_beats_the_window(self, auth: AuthService, user_id: uuid.UUID) -> None:
        """登出之後，寬限期內的那張也不能再換——撤銷永遠優先於便利。"""
        first = auth.login(tenant_id=TENANT_A, email=EMAIL, password=PASSWORD)
        current = auth.refresh(first.refresh_token)
        claims = get_token_codec().decode(current.access_token)

        auth.logout(
            tenant_id=TENANT_A,
            jti=claims.jti,
            access_expires_at=current.expires_at,
            refresh_token=current.refresh_token,
        )

        with pytest.raises(TokenRevokedError):
            auth.refresh(first.refresh_token)

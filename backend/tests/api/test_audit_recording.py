"""驗收：稽核紀錄真的被寫下來（04 §8.3、10 §3，2A-4）。

觸發面（開工前人類核可）：**寫入型請求預設全記、成功與失敗都記**。分類本身由
`tests/unit/test_audit_registry.py` 守門，這裡驗的是「分類之後真的有列落地，
而且欄位是對的」——兩者都要，因為註冊表寫得再漂亮，middleware 掛錯位置就一列
都寫不出來，且沒有任何症狀。

四件錯了都不會有例外的事：

1. **被拒的請求沒有留紀錄**（10 §3）。少了它，「有人在嘗試越權」與「某個角色的
   權限設錯了」在維運端長得一模一樣——都只是使用者說「我打不開」。
2. **稽核寫入失敗把主流程一起帶走**。稽核是旁路（同 usage）：記不成只能失去這
   一列，不能讓使用者的操作失敗。反過來也錯，所以失敗要留 log。
3. **before/after 存了整個物件**。稽核會被匯出、會被截圖、會進工單；password_hash
   躺在裡面的那一天不會有人發現（10 §5：secrets 不進 log、不進錯誤訊息）。
4. **request_id 對不上**。12 §1.1 的承諾是「一個 ID 查穿全鏈路」——稽核列上的
   request_id 必須就是回應標頭 `X-Request-Id` 的那一個，否則出事時稽核與存取
   日誌接不起來。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from apps.identity.models import User
from apps.platform.models import AuditLog
from common.passwords import hash_password
from repositories.platform import AuditLogRepository
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "another correct horse battery"
SLUG = "tenant-a"
OWNER_EMAIL = "owner@example.com"
VIEWER_EMAIL = "viewer@example.com"
USER_AGENT = "pytest-audit/1.0"


@pytest.fixture
def seeded() -> dict[str, uuid.UUID]:
    from apps.identity.models import Role

    ensure_identity_seed()
    ids: dict[str, uuid.UUID] = {}
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG, name="甲公司")
        owner = make_user(
            tenant_id=TENANT_A,
            email=OWNER_EMAIL,
            display_name="Owner",
            password_hash=hash_password(PASSWORD),
        )
        make_user_role(user=owner, role=Role.objects.get(tenant__isnull=True, name="owner"))
        viewer = make_user(
            tenant_id=TENANT_A,
            email=VIEWER_EMAIL,
            display_name="Viewer",
            password_hash=hash_password(PASSWORD),
        )
        make_user_role(user=viewer, role=Role.objects.get(tenant__isnull=True, name="viewer"))
        ids["owner"] = uuid.UUID(str(owner.id))
        ids["viewer"] = uuid.UUID(str(viewer.id))
    return ids


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    from api.main import create_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
        headers={"User-Agent": USER_AGENT},
    ) as c:
        yield c


async def _login(client: httpx.AsyncClient, email: str = OWNER_EMAIL) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": SLUG, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _rows(action: str | None = None) -> list[AuditLog]:
    """本租戶的稽核列，舊到新。**每一次登入也是一列**，所以計數一律先過濾。"""
    with tenant_scope(TENANT_A):
        query = AuditLog.objects.all().order_by("created_at")
        if action is not None:
            query = query.filter(action=action)
        return list(query)


def _only(action: str) -> AuditLog:
    rows = _rows(action)
    assert len(rows) == 1, f"預期 {action} 恰一列，實際 {len(rows)} 列"
    return rows[0]


class TestSuccessfulWrites:
    async def test_creating_a_user_is_recorded(
        self, client: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
    ) -> None:
        token = await _login(client)

        response = await client.post(
            "/api/v1/users",
            headers=_auth(token),
            json={"email": "new@example.com", "display_name": "New", "password": PASSWORD},
        )
        assert response.status_code == 201, response.text

        row = _only("user.create")
        assert row.actor_id == seeded["owner"]
        assert row.actor_type == "user"
        assert row.resource_type == "user"
        # 建立類的 id 不在 URL 上——service 經 core/audit.py 的 describe() 補。
        # 少了它，稽核只說得出「有人建了一個使用者」，說不出建了誰。
        assert row.resource_id == uuid.UUID(response.json()["id"])
        assert row.outcome == "succeeded"
        assert row.status == 201

    async def test_read_requests_are_not_recorded(self, client: httpx.AsyncClient) -> None:
        """GET 不留稽核：讀取量是寫入量的數十倍，混進來會讓真正的變更被淹沒。
        「誰讀了什麼」屬於存取日誌（12 §1.1），那裡已經有了。"""
        token = await _login(client)

        assert (await client.get("/api/v1/users", headers=_auth(token))).status_code == 200

        assert _rows("user.read") == []
        assert [row.action for row in _rows()] == ["auth.login"]

    async def test_exempt_endpoints_leave_no_row(self, client: httpx.AsyncClient) -> None:
        """例行的 token 輪換每 15 分鐘一次——記進稽核等於用噪音把訊號蓋掉。"""
        await _login(client)
        before = len(_rows())

        response = await client.post("/api/v1/auth/refresh")
        assert response.status_code == 200, response.text

        assert len(_rows()) == before

    async def test_the_row_carries_ip_user_agent_and_request_id(
        self, client: httpx.AsyncClient
    ) -> None:
        token = await _login(client)

        response = await client.patch(
            "/api/v1/tenants/current", headers=_auth(token), json={"name": "乙公司"}
        )
        assert response.status_code == 200, response.text

        row = _only("tenant.update")
        assert str(row.ip) == "127.0.0.1"
        assert row.user_agent == USER_AGENT
        assert row.request_id == response.headers["X-Request-Id"]


class TestFailedAndDeniedWrites:
    async def test_a_denied_request_records_the_permission_code(
        self, client: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
    ) -> None:
        """10 §3：被拒要記 audit **並帶上被拒的 permission code**。"""
        token = await _login(client, VIEWER_EMAIL)

        response = await client.post(
            "/api/v1/users",
            headers=_auth(token),
            json={"email": "nope@example.com", "display_name": "Nope", "password": PASSWORD},
        )
        assert response.status_code == 403, response.text

        row = _only("user.create")
        assert row.outcome == "denied"
        assert row.status == 403
        assert row.permission == "user:write"
        assert row.actor_id == seeded["viewer"]

    async def test_a_business_failure_is_recorded_as_failed(
        self, client: httpx.AsyncClient
    ) -> None:
        """404 也是稽核事實：「有人拿著不存在的 id 一直試」是可疑行為，
        而它與「前端傳錯參數」只有靠次數分得出來。"""
        token = await _login(client)

        response = await client.patch(
            f"/api/v1/users/{uuid.uuid4()}", headers=_auth(token), json={"display_name": "X"}
        )
        assert response.status_code == 404, response.text

        row = _only("user.update")
        assert row.outcome == "failed"
        assert row.status == 404

    async def test_unauthenticated_requests_leave_no_row(self, client: httpx.AsyncClient) -> None:
        """401 沒有租戶可歸屬（tenant_id NOT NULL + RLS），寫不進去也不該試。
        真正需要的那一半——登入失敗——由 AuthService 記，它知道租戶。"""
        response = await client.post(
            "/api/v1/users",
            json={"email": "x@example.com", "display_name": "X", "password": PASSWORD},
        )
        assert response.status_code == 401

        assert _rows() == []

    async def test_a_failing_audit_write_does_not_break_the_request(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """旁路原則（同 UsageService）：稽核記不成只能失去這一列。

        反過來的設計——稽核寫不進去就讓請求失敗——在「稽核表滿了」那天會讓
        整個平台停擺，而那正是最不該再加一個故障點的時刻。
        """
        token = await _login(client)

        def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("稽核表壞了")

        monkeypatch.setattr(AuditLogRepository, "add", boom)

        response = await client.post(
            "/api/v1/users",
            headers=_auth(token),
            json={"email": "still@example.com", "display_name": "Still", "password": PASSWORD},
        )

        assert response.status_code == 201, response.text


class TestBeforeAndAfter:
    """middleware 看得到誰、何時、做了什麼，看不到**改成什麼**——那要 service
    自己說（`core/audit.py` 的 describe）。這次只填真的會被問「誰改的、改成
    什麼」的三處。"""

    async def test_tenant_settings_change_records_both_sides(
        self, client: httpx.AsyncClient
    ) -> None:
        token = await _login(client)

        response = await client.patch(
            "/api/v1/tenants/current", headers=_auth(token), json={"name": "乙公司"}
        )
        assert response.status_code == 200, response.text

        row = _only("tenant.update")
        assert row.before == {"name": "甲公司"}
        assert row.after == {"name": "乙公司"}

    async def test_user_update_records_both_sides_without_secrets(
        self, client: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
    ) -> None:
        token = await _login(client)

        response = await client.patch(
            f"/api/v1/users/{seeded['viewer']}",
            headers=_auth(token),
            json={"display_name": "Renamed"},
        )
        assert response.status_code == 200, response.text

        row = _only("user.update")
        assert row.before == {"display_name": "Viewer"}
        assert row.after == {"display_name": "Renamed"}
        # 白名單而不是「整個物件減掉幾個欄位」：黑名單漏一個就是明文外洩，
        # 而新欄位是持續加的（10 §5）。
        serialized = f"{row.before}{row.after}"
        assert "password" not in serialized
        assert VIEWER_EMAIL not in serialized

    async def test_deactivating_a_user_records_the_state_change(
        self, client: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
    ) -> None:
        token = await _login(client)

        response = await client.post(
            f"/api/v1/users/{seeded['viewer']}/deactivate", headers=_auth(token)
        )
        assert response.status_code == 204, response.text

        row = _only("user.deactivate")
        assert row.before == {"is_active": True}
        assert row.after == {"is_active": False}

    async def test_deleting_a_knowledge_base_records_what_was_deleted(
        self, client: httpx.AsyncClient
    ) -> None:
        """刪除是唯一「事後查不到現場」的操作——資源已經不在了，稽核列上的
        before 是它存在過的唯一證據。"""
        token = await _login(client)
        created = await client.post(
            "/api/v1/knowledge-bases",
            headers=_auth(token),
            json={"name": "法規", "description": "內部規章"},
        )
        assert created.status_code == 201, created.text
        kb_id = created.json()["id"]

        response = await client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=_auth(token))
        assert response.status_code == 204, response.text

        row = _only("knowledge_base.delete")
        assert row.resource_id == uuid.UUID(kb_id)
        assert row.before == {"name": "法規"}
        assert row.after is None


class TestAuthEvents:
    """登入走 service 而不是 middleware：那條路徑上沒有 principal，
    也沒有租戶 contextvar（AuthService 自己進出 tenant_context）。"""

    async def test_successful_login_is_recorded(
        self, client: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
    ) -> None:
        await _login(client)

        row = _only("auth.login")
        assert row.actor_id == seeded["owner"]
        assert row.outcome == "succeeded"
        assert str(row.ip) == "127.0.0.1"
        assert row.request_id

    async def test_failed_login_is_recorded_without_the_password(
        self, client: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
    ) -> None:
        """密碼噴發的偵測完全靠這一列。而它同時是最容易把明文寫進 DB 的地方
        ——「順手記一下他打了什麼」在事後看來永遠是個壞主意。"""
        response = await client.post(
            "/api/v1/auth/login",
            json={"tenant_slug": SLUG, "email": OWNER_EMAIL, "password": "wrong password"},
        )
        assert response.status_code == 401

        row = _only("auth.login")
        assert row.outcome == "failed"
        assert row.actor_id == seeded["owner"]
        dumped = f"{row.before}{row.after}{row.action}{row.user_agent}"
        assert "wrong password" not in dumped

    async def test_logout_is_recorded(self, client: httpx.AsyncClient) -> None:
        """10 §2：session 撤銷屬敏感操作。"""
        token = await _login(client)

        response = await client.post("/api/v1/auth/logout", headers=_auth(token))
        assert response.status_code == 204, response.text

        assert _only("auth.logout").outcome == "succeeded"


class TestTenantIsolationOfWrites:
    async def test_the_row_lands_in_the_actors_tenant(
        self, client: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
    ) -> None:
        """稽核列的租戶來自 context，不是呼叫端自報（鐵則 4）。"""
        token = await _login(client)
        await client.patch("/api/v1/tenants/current", headers=_auth(token), json={"name": "丙"})

        with tenant_scope(TENANT_A):
            assert User.objects.filter(id=seeded["owner"]).exists()
        assert {row.tenant_id for row in _rows()} == {TENANT_A}

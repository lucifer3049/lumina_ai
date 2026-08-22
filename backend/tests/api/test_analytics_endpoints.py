"""驗收：`GET /analytics/usage`、`GET /analytics/costs`（09 §2.6，2A-3）。

Dashboard 的兩個問題：「用了多少」（requests／tokens）與「花了多少」（cost），
各自可按 day／user／model／category 分組。資料一律來自彙總表（掃分區表的理由
見 tests/integration/test_usage_rollup.py）——所以這裡的測試流程是完整迴路：
seed usage_logs → rollup → 打端點。

權限是**新的 code**（`analytics:read`，owner／admin）：用量輪廓是管理面資訊，
一般成員看得到自己的對話、看不到全租戶的消費統計（10 §RBAC 的「管公司」界線，
同 tenant:admin 的劃法但鬆一級——admin 看報表合理，改公司設定不行）。

三件事錯了都不會有例外：

1. **range 篩選在彙總後**才做（先加總再切），跨月查詢的邊界數字永遠對不上。
2. **viewer 拿得到**。403 的缺席不會有任何症狀，直到某個租戶的成員把全公司
   的用量截圖傳出去。
3. **cost 用 float 傳輸再相加**。單筆誤差極小，月報表加總後以「差幾分錢」現形。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from apps.platform.models import UsageLog
from common.passwords import hash_password
from core.db import run_orm
from services.platform.analytics import UsageRollupService
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG = "tenant-a"
OWNER_EMAIL = "owner@example.com"
VIEWER_EMAIL = "viewer@example.com"

_TODAY = datetime.now(UTC).date()
_USER_ID = uuid.uuid4()


def _seed_tenant() -> None:
    from apps.identity.models import Role

    ensure_identity_seed()
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG)
        owner = make_user(
            tenant_id=TENANT_A, email=OWNER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=owner, role=Role.objects.get(tenant__isnull=True, name="owner"))
        viewer = make_user(
            tenant_id=TENANT_A, email=VIEWER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=viewer, role=Role.objects.get(tenant__isnull=True, name="viewer"))


def _seed_usage(
    *,
    model: str = "mock-chat",
    category: str = "llm",
    prompt: int = 100,
    completion: int = 50,
    cost: str | None = "0.010000",
    days_ago: int = 0,
) -> None:
    with tenant_scope(TENANT_A):
        row = UsageLog.objects.create(
            tenant_id=TENANT_A,
            user_id=_USER_ID,
            category=category,
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cost=Decimal(cost) if cost is not None else None,
            request_id=str(uuid.uuid4()),
        )
        if days_ago:
            UsageLog.objects.filter(id=row.id, created_at=row.created_at).update(
                created_at=row.created_at - timedelta(days=days_ago)
            )


def _rollup(*days_ago: int) -> None:
    service = UsageRollupService()
    for offset in days_ago or (0,):
        service.rollup_tenant(TENANT_A, _TODAY - timedelta(days=offset))


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    from api.main import create_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


async def _token(client: httpx.AsyncClient, email: str = OWNER_EMAIL) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": SLUG, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


async def _get(client: httpx.AsyncClient, token: str, path: str, **params: Any) -> httpx.Response:
    return await client.get(
        f"/api/v1{path}", headers={"Authorization": f"Bearer {token}"}, params=params
    )


class TestUsageEndpoint:
    async def test_group_by_model_sums_the_buckets(self, client: httpx.AsyncClient) -> None:
        await run_orm(_seed_tenant)
        await run_orm(_seed_usage, model="mock-chat", prompt=100, completion=50)
        await run_orm(_seed_usage, model="mock-chat", prompt=30, completion=20)
        await run_orm(_seed_usage, model="other-model", prompt=10, completion=5)
        await run_orm(_rollup)
        token = await _token(client)

        response = await _get(
            client,
            token,
            "/analytics/usage",
            group_by="model",
            **{"from": str(_TODAY), "to": str(_TODAY)},
        )

        assert response.status_code == 200, response.text
        items = {item["key"]: item for item in response.json()["items"]}
        assert items["mock-chat"]["requests"] == 2
        assert items["mock-chat"]["prompt_tokens"] == 130
        assert items["mock-chat"]["completion_tokens"] == 70
        assert items["other-model"]["requests"] == 1

    async def test_group_by_day_keys_are_iso_dates(self, client: httpx.AsyncClient) -> None:
        await run_orm(_seed_tenant)
        await run_orm(_seed_usage, days_ago=1)
        await run_orm(_seed_usage, days_ago=0)
        await run_orm(_rollup, 0, 1)
        token = await _token(client)

        response = await _get(
            client,
            token,
            "/analytics/usage",
            group_by="day",
            **{"from": str(_TODAY - timedelta(days=1)), "to": str(_TODAY)},
        )

        keys = {item["key"] for item in response.json()["items"]}
        assert keys == {str(_TODAY - timedelta(days=1)), str(_TODAY)}

    async def test_the_range_filter_excludes_outside_days(self, client: httpx.AsyncClient) -> None:
        await run_orm(_seed_tenant)
        await run_orm(_seed_usage, days_ago=5)
        await run_orm(_seed_usage, days_ago=0)
        await run_orm(_rollup, 0, 5)
        token = await _token(client)

        response = await _get(
            client,
            token,
            "/analytics/usage",
            group_by="day",
            **{"from": str(_TODAY - timedelta(days=1)), "to": str(_TODAY)},
        )

        keys = {item["key"] for item in response.json()["items"]}
        assert keys == {str(_TODAY)}, "range 之外的日子不得出現"

    async def test_an_unknown_group_by_is_rejected(self, client: httpx.AsyncClient) -> None:
        await run_orm(_seed_tenant)
        token = await _token(client)

        response = await _get(client, token, "/analytics/usage", group_by="tenant")

        assert response.status_code == 422


class TestCostsEndpoint:
    async def test_group_by_model_sums_cost(self, client: httpx.AsyncClient) -> None:
        await run_orm(_seed_tenant)
        await run_orm(_seed_usage, cost="0.010000")
        await run_orm(_seed_usage, cost="0.035000")
        await run_orm(_seed_usage, cost=None)  # 缺價目：不計入 cost、但不該讓查詢爆掉
        await run_orm(_rollup)
        token = await _token(client)

        response = await _get(
            client,
            token,
            "/analytics/costs",
            group_by="model",
            **{"from": str(_TODAY), "to": str(_TODAY)},
        )

        assert response.status_code == 200, response.text
        items = {item["key"]: item for item in response.json()["items"]}
        assert Decimal(str(items["mock-chat"]["cost"])) == Decimal("0.045000")


class TestPermissions:
    async def test_viewer_is_denied(self, client: httpx.AsyncClient) -> None:
        """analytics:read 只給 owner／admin——用量輪廓是管理面資訊。"""
        await run_orm(_seed_tenant)
        token = await _token(client, VIEWER_EMAIL)

        response = await _get(client, token, "/analytics/usage")

        assert response.status_code == 403, response.text
        assert response.json()["code"] == "PERMISSION_DENIED"

    async def test_unauthenticated_is_rejected(self, client: httpx.AsyncClient) -> None:
        await run_orm(_seed_tenant)

        response = await client.get("/api/v1/analytics/usage")

        assert response.status_code == 401

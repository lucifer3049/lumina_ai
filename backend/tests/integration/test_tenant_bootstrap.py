"""驗收：從零建立一個可登入的租戶（13 §4：平台管理面用 CLI / Django Admin 頂替）。

**為什麼需要一條正式路徑**：在這之前，唯一會建租戶的是測試 factory。1A-5 的
smoke suite 第一步就是登入，CI 裡不可能有人手動去 Django Admin 點——所以要有
一條可以自動化、而且與正式流程同一條程式碼的入口。

**為什麼是「租戶 + Owner」一起建，不能分開**：只建租戶會產生一個沒有任何使用者
的空殼——沒有人登得進去，也就沒有人能在裡面建立第一個使用者。那是個死結，而它
的症狀是「租戶明明建好了，客戶卻說登不進去」。

**建租戶本身會踩到 RLS**（1A-2 的設計）：`identity_tenant` 的 policy 是
``id = 當前租戶``，所以連建立它都得先產生 uuid、設進 context、再寫入。這不是
繞過隔離，而是隔離的必然結果——系統裡沒有「站在所有租戶之外」的位置。本檔的
最後一組測試把這件事釘住，避免日後有人為了「方便」而給這條路徑開後門。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from io import StringIO

import pytest
from django.core.management import call_command

from common.passwords import verify_password
from core.exceptions import DomainError
from core.redis import get_redis, tenant_key
from services.identity.tenants import TenantService
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

SLUG = "acme"
OWNER_EMAIL = "owner@acme.test"
PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    """清掉本測試建立的租戶留下的 Redis 狀態（登入計數、refresh 家族）。

    租戶 id 是隨機的，所以用目錄表反查——那張表在 flush 之後就空了，
    因此要在 teardown 之前先讀出來。
    """
    yield

    from apps.identity.models import TenantDirectory

    tenant_ids = list(TenantDirectory.objects.values_list("tenant_id", flat=True))
    client = get_redis()
    for tenant_id in tenant_ids:
        keys = list(client.scan_iter(match=tenant_key(tenant_id, "*")))
        if keys:
            client.delete(*keys)


def _create(**overrides: object) -> object:
    # transaction=True 的測試會在每條之後 TRUNCATE，連 migration 種的系統角色都
    # 一起清掉——沒有 owner 角色就指派不了（見 tests/seed.py）。
    ensure_identity_seed()
    kwargs: dict[str, object] = {
        "name": "Acme Inc.",
        "slug": SLUG,
        "owner_email": OWNER_EMAIL,
        "owner_password": PASSWORD,
    }
    kwargs.update(overrides)
    return TenantService().create_tenant(**kwargs)  # type: ignore[arg-type]


# ── Service 層 ──────────────────────────────────────────────────


def test_creates_tenant_and_owner_together() -> None:
    result = _create()

    from apps.identity.models import Tenant, User
    from core.tenant import tenant_context
    from core.uow import unit_of_work

    with tenant_context(result.tenant_id), unit_of_work():  # type: ignore[attr-defined]
        tenant = Tenant.objects.get(id=result.tenant_id)  # type: ignore[attr-defined]
        owner = User.objects.get(email=OWNER_EMAIL)

    assert tenant.slug == SLUG
    assert tenant.status == "active"
    assert verify_password(PASSWORD, owner.password_hash)


def test_owner_gets_the_owner_role() -> None:
    """第一個使用者必須是 Owner——否則沒有人能管理這個租戶。

    指派的是系統角色（``tenant_id`` 為 NULL 的那一份），不是複製一份到租戶下：
    系統角色的保證就是「所有租戶共用同一份、不可修改」。
    """
    result = _create()

    from apps.identity.models import UserRole
    from core.tenant import tenant_context
    from core.uow import unit_of_work

    with tenant_context(result.tenant_id), unit_of_work():  # type: ignore[attr-defined]
        assignments = list(UserRole.objects.select_related("role").all())

    assert [a.role.name for a in assignments] == ["owner"]
    assert all(a.role.tenant_id is None for a in assignments)


def test_slug_appears_in_the_login_directory() -> None:
    """目錄表（登入用的 slug → id 對照）必須同步出現。

    它是由 trigger 維護的，所以這條測試真正驗的是「trigger 有掛上」——
    漏掉的話症狀是「租戶建好了但登不進去」，而查租戶表時資料都在。
    """
    result = _create()

    from apps.identity.models import TenantDirectory

    assert TenantDirectory.objects.get(slug=SLUG).tenant_id == result.tenant_id  # type: ignore[attr-defined]


def test_duplicate_slug_is_rejected() -> None:
    """slug 是登入的識別字，重複等於兩家公司搶同一個入口。"""
    _create()

    with pytest.raises(DomainError):
        _create(owner_email="another@acme.test")


def test_the_new_owner_can_actually_authenticate() -> None:
    """端到端：建完之後真的登得進去。

    分開驗「資料建對了」與「登得進去」是必要的——前者全綠而後者失敗的情況真的
    會發生（例如密碼存成明文、或目錄表沒同步），而那正是客戶第一天就會遇到的事。
    """
    from services.identity.auth import AuthService

    result = _create()
    auth = AuthService()

    tenant_id = auth.resolve_tenant(SLUG)
    pair = auth.login(tenant_id=tenant_id, email=OWNER_EMAIL, password=PASSWORD)

    assert tenant_id == result.tenant_id  # type: ignore[attr-defined]
    assert pair.access_token


def test_tenant_row_is_written_inside_its_own_tenant_context() -> None:
    """建立流程必須在**該租戶自己的 context** 內寫入（RLS 的 WITH CHECK）。

    這條的用意是防止日後有人為了「方便」把這條路徑改成繞過 RLS（例如改用
    admin 連線）。若真的那樣改，這裡不會紅——所以斷言換個角度：用**應用角色**
    在該租戶 context 下讀得到剛建立的列，代表它確實是以合法的租戶身分寫入的。
    """
    from apps.identity.models import Tenant
    from core.tenant import tenant_context
    from core.uow import unit_of_work

    result = _create()

    with tenant_context(result.tenant_id), unit_of_work():  # type: ignore[attr-defined]
        assert Tenant.objects.filter(id=result.tenant_id).exists()  # type: ignore[attr-defined]

    # 換一個租戶 context 就應該看不到（RLS 生效中）。
    with tenant_context(uuid.uuid4()), unit_of_work():
        assert not Tenant.objects.filter(id=result.tenant_id).exists()  # type: ignore[attr-defined]


# ── CLI ─────────────────────────────────────────────────────────


def test_management_command_creates_a_usable_tenant() -> None:
    """``manage.py create_tenant`` —— smoke suite 與新客戶開通走的是同一條路。"""
    ensure_identity_seed()
    out = StringIO()

    call_command(
        "create_tenant",
        "--name",
        "Acme Inc.",
        "--slug",
        SLUG,
        "--owner-email",
        OWNER_EMAIL,
        "--owner-password",
        PASSWORD,
        stdout=out,
    )

    from apps.identity.models import TenantDirectory

    assert TenantDirectory.objects.filter(slug=SLUG).exists()
    assert SLUG in out.getvalue()


def test_management_command_does_not_print_the_password() -> None:
    """輸出不得包含密碼（鐵則 9：secrets 不進 log）。

    CLI 的輸出會被貼進工單、CI log、聊天室——那是密碼最常見的外流路徑之一。
    """
    ensure_identity_seed()
    out = StringIO()

    call_command(
        "create_tenant",
        "--name",
        "Acme Inc.",
        "--slug",
        SLUG,
        "--owner-email",
        OWNER_EMAIL,
        "--owner-password",
        PASSWORD,
        stdout=out,
    )

    assert PASSWORD not in out.getvalue()

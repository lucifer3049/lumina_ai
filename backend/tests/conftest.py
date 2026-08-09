"""測試共用 fixture。

**雙租戶是預設，不是選項**（CLAUDE.md 測試規範）：只有一個租戶的測試無法
證明隔離有效——查詢當然只會回傳那個租戶的資料。所以 fixture 一律建兩個租戶
並各自塞資料，隔離斷言才有意義。

**兩條 DB 連線**（13 §3.1 的 1A-P2）：``default`` 是應用角色、``admin`` 是
schema owner。見下方 :func:`django_db_setup` 的說明。
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator

import pytest
from django.db import connections
from pytest_django import DjangoDbBlocker

from core.tenant import tenant_context
from tests.factories.identity import make_tenant, make_user, tenant_scope

# ── 環境守門（第二道；第一道在 Makefile 頂部）────────────────────────
# 本專案統一在 WSL2 開發。從 Windows 側跑 `uv run pytest` 時，uv 在 pytest 啟動
# **之前**就已把 WSL2 建的 .venv 砍掉重建（跨平台 venv 不相容）——這裡攔不回來，
# 但能把「為什麼測試環境突然壞掉」講清楚，而不是讓人繼續在 Windows 上開發到
# venv 變成殘骸（2026-08-03 實際發生）。CI（ubuntu）與 WSL2 都是 linux，不受影響。
#
# 訊息刻意寫成**純 ASCII 英文**：Windows 主控台預設是本地 codepage（繁中為
# cp950），中文訊息在那裡會整段變成亂碼——而這正是唯一會看到這則訊息的平台。
# 上面的中文註解給讀原始碼的人看，那是在編輯器裡，不受 codepage 影響。
# （2026-08-03 實測：原本的中文版在 Windows console 呈現為「���M�ײΤ@...」。）
if sys.platform == "win32":
    raise pytest.UsageError(
        "This project must be developed inside WSL2; run the tests there, not from Windows. "
        "The uv call you just made has already rebuilt backend/.venv for Windows. "
        "Back in WSL2 the first command will rebuild it again (plain `uv sync`, no data loss)."
    )

TENANT_A = uuid.UUID("11111111-1111-5111-8111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-5222-8222-222222222222")


def _grant_truncate_for_transactional_tests() -> None:
    """只在 **test database** 給應用角色 TRUNCATE（`transactional_db` 的需求）。

    pytest-django 的 ``transaction=True`` 測試在每條之後跑 Django 的 ``flush``，
    而 flush 是 ``TRUNCATE``——它落在測試宣告的 alias 上，也就是應用角色。

    **這個權限絕不能進 initdb.d**（那會套到開發與正式環境）：``TRUNCATE`` 完全
    不受 RLS policy 約束，一個帶 TRUNCATE 的應用角色可以清掉其他租戶的資料，
    而 policy 攔不住——那正是角色拆分要防的東西。所以它只存在於 test database，
    生命週期跟著 test database 一起結束。

    對測試保真度的影響是明確的一項偏離：測試中的應用角色比正式環境多一個
    TRUNCATE。可接受的理由是它不影響任何 SELECT/INSERT/UPDATE/DELETE 路徑的
    權限與 RLS 行為，而那些才是隔離測試要驗的對象。
    ``tests/unit/test_db_role_config.py`` 反向釘住 initdb.d 不得出現 TRUNCATE。
    """
    app_user = connections["default"].settings_dict["USER"]
    with connections["admin"].cursor() as cursor:
        cursor.execute(f'GRANT TRUNCATE ON ALL TABLES IN SCHEMA public TO "{app_user}"')
        cursor.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT TRUNCATE ON TABLES TO "{app_user}"'
        )


@pytest.fixture(scope="session")
def django_db_setup(
    request: pytest.FixtureRequest,
    django_test_environment: None,
    django_db_blocker: DjangoDbBlocker,
    django_db_keepdb: bool,
) -> Iterator[None]:
    """建立 test database，且**以 owner 角色**建（13 §3.1 的 1A-P2）。

    為什麼要覆寫 pytest-django 的預設版本：``django.test.utils.setup_databases``
    會把簽章相同的 alias 併成一組，而簽章是 ``(HOST, PORT, ENGINE, NAME)``
    ——**不含 USER**。``default`` 與 ``admin`` 指向同一個資料庫，於是被併成一組，
    而 Django 明文把 ``default`` 排在第一個（資料 migration 預設走 default），
    建庫就會落到應用角色頭上。應用角色沒有 ``CREATEDB``（給了它就等於讓它能自建
    一個不受任何 policy 保護的資料庫），整套測試會在收集完成後直接死在建庫。

    所以這裡只讓 ``admin`` 走 ``setup_databases``（建庫 + 跑 migration，於是每張表
    的 owner 都是 owner 角色），再把 ``default`` 設成它的 test mirror。
    ``set_as_test_mirror`` 只複製 ``NAME``，**不動 USER**——兩條連線因此指向同一個
    test database 卻各自是不同角色，這正是 RLS 測試需要的形狀。

    ``django_db_modify_db_settings`` 沒有列進相依（pytest-xdist 的資料庫名後綴靠
    它）：本專案不跑 xdist；要跑的話這裡要一併處理，否則各 worker 會共用同一個
    test database。
    """
    from django.test.utils import setup_databases, teardown_databases

    verbosity = request.config.option.verbose

    with django_db_blocker.unblock():
        db_cfg = setup_databases(
            verbosity=verbosity,
            interactive=False,
            # Django 只用 `alias in aliases`，但它自己傳的是 mapping
            # （`DiscoverRunner.get_databases()` 回 alias → 是否序列化），型別註記
            # 也是 Mapping。跟著傳 mapping，不倚賴「set 剛好也能用」。
            aliases={"admin": False},
            keepdb=django_db_keepdb,
        )
        connections["default"].creation.set_as_test_mirror(connections["admin"].settings_dict)
        _grant_truncate_for_transactional_tests()

    yield

    if not django_db_keepdb:
        with django_db_blocker.unblock():
            # `teardown_databases` 只關掉它自己建的那條連線（admin）。default 是
            # 後來被指過去的 mirror，Django 不知道它存在，於是 DROP DATABASE 會撞上
            # `database "test_lumina" is being accessed by other users`——而那是在
            # 整個 session 結束後才爆，訊息裡沒有任何線索指向 mirror。
            connections.close_all()
            teardown_databases(db_cfg, verbosity=verbosity)


@pytest.fixture
def two_empty_tenants(transactional_db: object) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """只建兩個租戶列，不建任何使用者。

    給「要自己寫入再驗證回滾/提交」的測試用（tests/integration/test_uow.py）：
    那些測試的斷言是「事後應該一筆都沒有」，fixture 先塞資料會讓它們永遠失敗。

    用 ``transactional_db`` 而非 ``db``：run_orm 把查詢送到另一條執行緒，
    pytest-django 預設的交易包裹在那頭看不到，測試會以假失敗誤導人。
    """
    for tenant_id, name in ((TENANT_A, "a"), (TENANT_B, "b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=f"tenant-{name}")
    yield TENANT_A, TENANT_B


@pytest.fixture
def two_tenants(
    two_empty_tenants: tuple[uuid.UUID, uuid.UUID],
) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """兩個租戶各 3 個使用者——隔離斷言的標準載具。

    1A-5 之前這裡用的是 ``apps.spike`` 的 ``SpikeItem``（一張沒有 RLS 的玩具表）。
    改用 ``identity_user`` 之後，同一組測試順帶多驗一件事：**寫入路徑本身受 RLS
    約束**（建立資料必須在租戶 context ＋ 交易內，見 factories 的 `tenant_scope`），
    而玩具表驗不到那一半。
    """
    for tenant_id, name in ((TENANT_A, "a"), (TENANT_B, "b")):
        with tenant_scope(tenant_id):
            for i in range(3):
                make_user(tenant_id=tenant_id, email=f"{name}-{i}@example.com")
    yield TENANT_A, TENANT_B


@pytest.fixture
def as_tenant_a() -> Iterator[uuid.UUID]:
    with tenant_context(TENANT_A) as tid:
        yield tid

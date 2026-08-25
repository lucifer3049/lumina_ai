"""測試共用 fixture。

**雙租戶是預設，不是選項**（CLAUDE.md 測試規範）：只有一個租戶的測試無法
證明隔離有效——查詢當然只會回傳那個租戶的資料。所以 fixture 一律建兩個租戶
並各自塞資料，隔離斷言才有意義。

**兩條 DB 連線**（13 §3.1 的 1A-P2）：``default`` 是應用角色、``admin`` 是
schema owner。見下方 :func:`django_db_setup` 的說明。
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Iterator

# ── pytest-xdist 的 worker 分割（必須在任何專案 import 之前）────────
# test database 靠 pytest-django 的 `_gwN` 後綴自動分開，但那只涵蓋 PostgreSQL。
# 另外兩個共用資源要在這裡處理，否則 worker 之間會互相踩：
#
# 1. Redis——登入失敗計數與 token 撤銷名單。每個 worker 一個邏輯 DB。
# 2. 物件儲存——MinIO 只有一個 bucket，且應用角色**刻意沒有** s3:CreateBucket
#    （docker/compose.yml 的 minio-init 有反向驗收測試守著），所以不能每個 worker
#    一個 bucket。改為每個 worker 一組租戶 UUID：物件 key 是
#    `tenant-{tenant_id}/kb/{kb_id}/{document_id}`，租戶一分開，前綴就分開了。
#
# 租戶分割同時也讓 Redis 的 key（`t:{tenant_id}:...`）天然分開；Redis 的邏輯 DB
# 仍然留著，因為不是每個 key 都保證帶租戶前綴，而多這一層的成本是零。
#
# **只有 Redis 那一半必須早於 import**：`get_app_settings()` 帶 lru_cache，第一次讀
# 到什麼就固定成什麼，而環境變數的優先序高於 .env（見
# config/settings/app_settings.py）。租戶 UUID 是純常數，跟著其他常數放在 import
# 之後即可——把賦值搬到這裡只會讓後面每一行 import 都吃 E402。
#
# 序列跑用 0 號。Redis 預設只有 16 個邏輯 DB，超出就明確失敗——不 fail 的話 worker
# 會靜默共用 0 號，也就是回到這段要解決的問題本身。
if _worker := os.environ.get("PYTEST_XDIST_WORKER"):
    # 上限 14 而不是 15：**15 保留給 smoke**（`tests/e2e/conftest.py` 的
    # `_SMOKE_REDIS_DB`），0 是 dev 與 `make start` 在用的。撞在一起的話，兩邊的
    # Celery worker 會搶同一個工作籃，而症狀完全不指向真因（見那份 conftest 的說明）。
    if int(_worker.removeprefix("gw")) + 1 > 14:
        raise RuntimeError(
            f"pytest-xdist worker {_worker} 超出可用的 Redis 邏輯 DB（1~14；"
            "0 給 dev、15 給 smoke）；降低 PYTEST_XDIST_N，或改用獨立的 Redis 實例"
        )
    os.environ["REDIS_DB"] = str(int(_worker.removeprefix("gw")) + 1)

# **HTTP 頻率限制在測試裡預設關掉**（二次架構審計 F-11）。同樣必須早於 import：
# `get_app_settings()` 帶 lru_cache。
#
# 理由不是「限流會擋到測試」這麼簡單——它會**隨機**擋到測試：桶是 per-IP 的，而
# ASGITransport 底下每一條 api 測試都是同一個 127.0.0.1；認證桶是 20/分鐘，而幾乎
# 每條 api 測試都先登入一次。於是同一份程式碼會依「這一分鐘剛好跑了幾條測試」而
# 紅或綠，看起來像 flaky，實際上是限流正常運作。
#
# 要驗限流本身的測試自己把它打開（tests/api/test_rate_limit.py），那也讓「這條
# 測試在驗限流」這件事在檔案裡看得見。
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

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

# 租戶 UUID 的最後兩碼是 worker 序號（序列跑為 00），理由見檔案開頭的 worker 分割
# 說明：物件儲存只有一個 bucket，租戶前綴就是 worker 之間的邊界。
# 其餘位元保持原樣，version（5）與 variant（8）兩個 nibble 也不動——這兩個欄位有
# 語意，隨手改會讓「看起來像 UUID 的字串」變成不合法的 UUID，而 psycopg 與
# pydantic 都會擋。
_WORKER_INDEX = int(os.environ.get("PYTEST_XDIST_WORKER", "gw-1").removeprefix("gw")) + 1

TENANT_A = uuid.UUID(f"11111111-1111-5111-8111-1111111111{_WORKER_INDEX:02x}")
TENANT_B = uuid.UUID(f"22222222-2222-5222-8222-2222222222{_WORKER_INDEX:02x}")


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
    django_db_modify_db_settings: None,
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

    ``django_db_modify_db_settings`` **必須**列進相依：pytest-xdist 的每個 worker
    要有自己的 test database，而那個 ``_gwN`` 後綴就是這個 fixture 加到
    ``settings_dict`` 上的。少了它，六個 worker 會共用同一個 test database——症狀
    不是報錯而是隨機失敗：A worker 的 ``transactional_db`` 在測試之間跑 TRUNCATE，
    把 B worker 正在讀的資料清掉。它先於 ``setup_databases`` 求值，因此建出來的庫
    名已經帶後綴；``set_as_test_mirror`` 隨後把同一個名字複製給 ``default``，兩條
    連線仍落在同一個 worker 專屬的庫上。
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


def _wipe_tenant_redis() -> None:
    from core.redis import get_redis, tenant_key

    client = get_redis()
    for tenant_id in (TENANT_A, TENANT_B):
        keys = list(client.scan_iter(match=tenant_key(tenant_id, "*")))
        if keys:
            client.delete(*keys)


@pytest.fixture(autouse=True)
def _isolated_redis(request: pytest.FixtureRequest) -> Iterator[None]:
    """每條測試**前後**都清掉本 worker 兩個租戶的 Redis key。

    **Redis 是同一個 worker 內唯一沒有人幫我們回滾的共用狀態。** DB 有 pytest-django
    的交易回滾與 flush，物件儲存靠租戶前綴分開（見檔案開頭），而 Redis 裡的登入失敗
    計數（15 分鐘 TTL）、jti 撤銷名單與 refresh 家族全部是**跨測試存活**的——同一個
    worker 的下一條測試會直接看到它們。

    在這之前，這件事是由各個測試檔各自寫一份 teardown fixture 處理的（十來份幾乎一樣
    的程式碼）。那個做法有兩個問題，而且第二個更嚴重：

    1. **新檔案必須記得抄一份**，忘了就沒有；而忘了的症狀不會出現在那個檔案上。
    2. **只清 teardown 等於假設「別人也都有清」**。一條測試若在髒的狀態下開始，受害的
       是它，而肇事的是別人——排錯時看到的堆疊完全指向錯的地方。

    **清在進入時而不是離開時**：那是這條測試自己控制得了的一端。每條測試都清進入端
    之後，離開端就是多餘的（下一條進來時反正會再清一次），而它要付的是每條測試多一
    次 Redis 往返——全套 800 多條測試量得出來（實測約 +5 秒）。既有的各檔 teardown
    fixture 因此不必動，但新檔案**不需要**再抄一份。

    這是針對**這一類**問題的處置，不是針對某一次的重現：2026-08-16 的一次全套執行有
    兩條互不相關的測試同時紅（`test_refresh_rotation` 與 `test_knowledge_permissions`
    的權限矩陣），而該次沒有任何 teardown ERROR（因此不是 1D-2 修掉的 flush 那一類），
    之後六次重跑全綠、未能重現。共通點是兩者都依賴「登入拿得到 token」，而那正好是
    Redis 裡唯一跨測試存活的狀態。

    unit 層跳過：`make test-unit` 明說不需要 `make up`（無外部依賴），而連 Redis 就是
    給它加一個依賴。
    """
    if "unit" in request.node.path.parts:
        yield
        return
    _wipe_tenant_redis()
    yield


@pytest.fixture(autouse=True)
def _unit_layer_has_no_redis(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """unit 層真的連上 Redis 時，當場失敗並說出原因。

    `make test-unit` 的定義是「無外部依賴，不需 make up」（Makefile 與上面
    `_isolated_redis` 的說明），而 CI 的 quality job 據此**不起任何服務**。問題在於
    這個約定沒有東西守著：開發機的 Redis 一直開著，於是新加的相依在本機是全綠的，
    紅燈只出現在 CI——而且訊息是 `ConnectionError: Connection refused`，指向 Redis
    而不是指向「這條測試不該碰 Redis」。2A-2b 的公平閘就是這樣進來的（4 條測試
    連紅，本機重跑全綠）。

    **攔在 `Redis.execute_command` 而不是 `core.redis.get_redis`**：呼叫端多半寫
    `from core.redis import get_redis`，那個名字在 import 期就綁好了，補 module
    屬性攔不到。所有 Redis 指令最後都經過 `execute_command`，補在那裡才是每條路徑
    都涵蓋得到的那一層。

    要 Redis 的測試不是不能寫——是不屬於 unit 層。把它移到 integration/api（那裡
    有 `make up` 的服務），或把該相依 stub 掉。
    """
    if "unit" not in request.node.path.parts:
        return

    from typing import NoReturn

    from redis import Redis
    from redis.asyncio import Redis as AsyncRedis

    def _forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError(
            "unit 層不得連 Redis（make test-unit 無外部依賴，CI 的 quality job "
            "不起任何服務）。把這條測試移到 tests/integration，或把碰 Redis 的"
            "相依 stub 掉。"
        )

    monkeypatch.setattr(Redis, "execute_command", _forbidden)
    monkeypatch.setattr(AsyncRedis, "execute_command", _forbidden)


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

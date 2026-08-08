"""驗收：角色拆分在**設定面**的接線（13 §3.1 的 1A-P1~P3）。

與 ``tests/integration/test_db_roles.py`` 的分工：那一檔驗「DB 上實際生效的權限」，
本檔驗「產生那個狀態的設定有沒有寫對」。兩者都要，因為它們的失敗模式不同：

- 只有 integration：本機資料卷是幾週前建的，設定改壞了也照樣綠（角色早就存在），
  ``make clean`` 重建後才炸——而重建通常發生在別人的機器或 CI 上。
- 只有 unit：設定看起來對，但 ``make db-timeouts`` 沒跑、或 migration 誤走
  default alias，DB 上仍是錯的。

本檔全部是純文字/設定斷言，不需要 ``make up``。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from django.conf import settings

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "docker" / "compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
MAKEFILE = REPO_ROOT / "Makefile"
ROLES_SCRIPT = REPO_ROOT / "docker" / "postgres" / "initdb.d" / "10-roles.sh"

# .env.example 必須新增的三組值（.env 由使用者自行複製後填）
REQUIRED_ENV_KEYS = (
    "DB_ADMIN_USER",
    "DB_ADMIN_PASSWORD",
    "POSTGRES_SUPERUSER",
    "POSTGRES_SUPERUSER_PASSWORD",
)


def _env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


# ── .env.example：三組憑證各自獨立 ──────────────────────────────


def test_env_example_declares_the_three_database_accounts() -> None:
    """superuser / owner / 應用三組帳號都要在樣板裡（新人照抄即可起環境）。"""
    missing = [key for key in REQUIRED_ENV_KEYS if key not in _env_example()]

    assert not missing, f".env.example 缺少角色拆分所需的變數：{missing}"


def test_the_three_database_accounts_are_distinct() -> None:
    """三組帳號名與密碼都不得重複。

    重複的後果不是「權限稍微寬一點」而是**拆分完全不存在**：帳號同名時
    應用連的就是 owner；密碼相同時拿到應用憑證即可用 owner 身分登入。
    兩者都沒有症狀。
    """
    values = _env_example()
    users = [values["POSTGRES_SUPERUSER"], values["DB_ADMIN_USER"], values["DB_USER"]]
    passwords = [
        values["POSTGRES_SUPERUSER_PASSWORD"],
        values["DB_ADMIN_PASSWORD"],
        values["DB_PASSWORD"],
    ]

    assert len(set(users)) == 3, f"三個 DB 帳號名有重複：{users}"
    assert len(set(passwords)) == 3, "三個 DB 密碼有重複——帳號分開但密碼共用等於沒分開"


# ── compose：應用帳號不得是 initdb superuser ────────────────────


def test_postgres_initdb_user_is_not_the_application_account() -> None:
    """``POSTGRES_USER`` 必須指向 superuser 變數，不得再是 ``DB_USER``（1A-P1）。

    initdb 建出來的帳號是 superuser 兼 schema owner。它同時當應用連線帳號時，
    RLS policy 對應用完全不適用——而 ``POSTGRES_USER`` 只在 initdb 生效，
    改它要 ``make clean`` 重建資料卷，所以這條在 1A 開頭就要釘死。
    """
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    initdb_user = re.search(r"^\s*POSTGRES_USER:\s*(\S+)", compose, re.MULTILINE)

    assert initdb_user is not None, "compose.yml 找不到 POSTGRES_USER"
    assert initdb_user.group(1) == "${POSTGRES_SUPERUSER}", (
        f"POSTGRES_USER 目前是 {initdb_user.group(1)}——initdb 帳號是 superuser，"
        "不得同時是應用連線帳號（05 §5.1）"
    )


def test_application_role_is_never_granted_truncate() -> None:
    """initdb.d 授予應用角色的權限清單不得含 ``TRUNCATE``。

    ``TRUNCATE`` **完全不受 RLS policy 約束**——帶著它的應用角色可以清掉其他租戶
    的資料，而 policy 攔不住。這是 RLS 的第五條繞道，而且它不在
    ``tests/integration/test_db_roles.py`` 的角色屬性檢查範圍內（那查的是 rolsuper
    這類 cluster 級屬性，不是表級授權）。

    測試環境是唯一例外：`transactional_db` 的 flush 就是 TRUNCATE，所以
    ``tests/conftest.py`` 只在 test database 內補這個權限，理由寫在該函式的
    docstring。這條測試守的是「那個例外不要外溢到 initdb.d」。
    """
    script = ROLES_SCRIPT.read_text(encoding="utf-8")
    granting_lines = [
        line
        for line in script.splitlines()
        if "GRANT" in line and "TRUNCATE" in line.upper() and not line.lstrip().startswith("#")
    ]

    assert not granting_lines, (
        f"initdb.d 授予了 TRUNCATE：{granting_lines}——TRUNCATE 繞過 RLS policy，"
        "應用角色拿到它就能清掉其他租戶的資料"
    )


# ── Makefile：migration 與 timeout 各自套對角色 ─────────────────


def test_migrate_runs_as_the_owner_role() -> None:
    """``make migrate`` 必須顯式走 admin alias。

    Django 預設用 default alias，而 default 拆分後是應用角色——它沒有 DDL 權限。
    症狀是 migration 直接失敗（這是好的）；真正危險的是有人為了讓它過而把
    CREATE 權限補回應用角色，那會一次廢掉 ``test_application_role_owns_no_table``
    想守的東西。
    """
    migrate_target = re.search(
        r"^migrate:.*?\n((?:\t.*\n)+)", MAKEFILE.read_text(encoding="utf-8"), re.MULTILINE
    )

    assert migrate_target is not None, "Makefile 找不到 migrate target"
    assert "--database=admin" in migrate_target.group(1), (
        "make migrate 未指定 --database=admin → migration 會以應用角色執行"
    )


def test_statement_timeout_is_applied_to_the_application_role_only() -> None:
    """``ALTER ROLE ... SET statement_timeout`` 的對象必須是應用角色（1A-P3）。

    目前套在 ``POSTGRES_USER`` 上，而該角色拆分後負責跑 migration——5s 上限會
    砍掉 ``AddIndexConcurrently`` 與 HNSW 建索引，留下半套 schema。
    """
    makefile = MAKEFILE.read_text(encoding="utf-8")
    apply_block = re.search(
        r"^APPLY_DB_TIMEOUTS\s*=(.*?)(?=\n\S)", makefile, re.DOTALL | re.MULTILINE
    )

    assert apply_block is not None, "Makefile 找不到 APPLY_DB_TIMEOUTS"
    body = apply_block.group(1)

    assert "statement_timeout" in body, "APPLY_DB_TIMEOUTS 不再設定 statement_timeout？"
    assert "POSTGRES_USER" not in body.split("ALTER ROLE")[-1], (
        "statement_timeout 仍套在 POSTGRES_USER（superuser / migration 角色）上——"
        "會砍掉 AddIndexConcurrently 與 HNSW 建索引"
    )
    assert "DB_USER" in body, "statement_timeout 未套用到應用角色（DB_USER）"


# ── Django settings：兩條連線各走各的埠 ────────────────────────


def test_admin_alias_exists_and_uses_the_owner_account() -> None:
    """``admin`` alias = owner 角色，供 migration 與 pytest 建庫使用（1A-P2）。"""
    assert "admin" in settings.DATABASES, (
        "settings.DATABASES 缺 `admin` alias——pytest 需要 CREATE DATABASE，而該權限不能給應用角色"
    )
    assert settings.DATABASES["admin"]["USER"] == os.environ["DB_ADMIN_USER"]
    assert settings.DATABASES["default"]["USER"] == os.environ["DB_USER"]
    assert settings.DATABASES["admin"]["USER"] != settings.DATABASES["default"]["USER"], (
        "兩條連線用同一個帳號 → RLS 測試驗到的不是應用角色的行為（1A-P2）"
    )


def test_admin_alias_bypasses_pgbouncer() -> None:
    """owner 連線直連 PostgreSQL，不經 PgBouncer。

    兩個理由，任一單獨成立即足夠：``CREATE DATABASE`` 無法經 transaction mode 的
    連線池（它綁定固定 dbname）；migration 的 ``CREATE INDEX CONCURRENTLY`` 與
    advisory lock 在 transaction pooling 下語意會壞掉（05 §5.5）。
    """
    admin = settings.DATABASES["admin"]

    assert str(admin["PORT"]) == os.environ["DB_DIRECT_PORT"], (
        f"admin alias 的埠是 {admin['PORT']}，應為直連埠 {os.environ['DB_DIRECT_PORT']}"
    )
    assert admin["CONN_MAX_AGE"] == 0, (
        "admin 連線不該重用：它只在 migration / 建庫時短暫使用，長連線會在 PG 端佔著一條特權連線"
    )

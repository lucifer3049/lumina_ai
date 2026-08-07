"""驗收：PgBouncer 設定渲染（docker/pgbouncer/render.sh）。

兩組驗收混在同一支檔案裡，因為它們驗的是同一份產出：

**A. 特殊字元密碼必須字面取代。** 這條來自一個實際踩到的坑：原本用
`sed "s|__DB_PASSWORD__|$DB_PASSWORD|g"` 注入密碼，而 sed 會把替換字串當運算式解讀——

- 密碼含 `&`：sed 展開成「整段匹配文字」，產出的設定帶著**錯誤密碼且完全不報錯**，
  症狀是 pgbouncer 認證失敗，指向完全錯誤的原因（會去查網路、查 PG 的 pg_hba）。
- 密碼含 `|`：`sed: bad option in substitution expression`，容器直接掛。
- awk 的 `gsub()` 有同樣的 `&` 問題，所以 render.sh 用 index/substr 做字面取代。

**B. 管理主控台與應用帳號必須分離。** `admin_users` 的帳號可以對連線池下
`PAUSE` / `KILL` / `RELOAD`；把應用帳號放進去，等於誰拿到應用的 DB 密碼就能讓全站
連不上。壓測要看的 `SHOW POOLS` 屬唯讀，走 `stats_users` 就夠——
兩者分開之後，既有的觀測流程不受影響，危險操作才需要另一組憑證。

PgBouncer 的管理帳號**不需要存在於 PostgreSQL**（它認的是 userlist.txt，
連的是虛擬的 `pgbouncer` database），所以這個分離不必動任何 PG role。

測試直接跑那支 shell script（sh + awk，CI 與 WSL 皆有），不經 docker——
要驗的是取代邏輯，起容器只會讓這條測試慢十倍且需要外部依賴。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RENDER_SH = REPO_ROOT / "docker" / "pgbouncer" / "render.sh"

DB_USER = "lumina"
ADMIN_USER = "pgbouncer_admin"

# 三種會讓「把值塞進替換運算式」的作法出錯的字元，一次全帶上。
# 兩個帳號給**不同**的毒字串：值互換或共用同一個變數時才會被抓到。
NASTY_PASSWORD = r"p&w|d\ss'\"x"
NASTY_ADMIN_PASSWORD = r"a&m|n\ss'\"y"

_INI_SETTING = re.compile(r"^\s*([a-z_]+)\s*=\s*(.*?)\s*$", re.MULTILINE)


def _render(out_dir: Path, **overrides: str) -> subprocess.CompletedProcess[bytes]:
    """跑 render.sh；overrides 為 None 的鍵會被移除（用來驗缺變數的行為）。"""
    env = {
        "PATH": "/usr/bin:/bin",
        "DB_USER": DB_USER,
        "DB_PASSWORD": NASTY_PASSWORD,
        "DB_NAME": "lumina",
        "PGBOUNCER_ADMIN_USER": ADMIN_USER,
        "PGBOUNCER_ADMIN_PASSWORD": NASTY_ADMIN_PASSWORD,
        "TPL_DIR": str(RENDER_SH.parent),
        "OUT_DIR": str(out_dir),
    }
    env.update(overrides)

    return subprocess.run(  # noqa: S603 —— 固定指令、參數不來自外部輸入
        ["sh", str(RENDER_SH)],  # noqa: S607
        capture_output=True,
        env=env,
        check=False,
    )


def _ini_users(content: str, key: str) -> set[str]:
    """取 pgbouncer.ini 中某個帳號清單設定（`;` 起首為註解，正則的 `^[a-z_]+` 自然排除）。"""
    values = [value for name, value in _INI_SETTING.findall(content) if name == key]
    assert values, f"pgbouncer.ini 沒有設定 `{key}`"
    return {user.strip() for user in values[-1].split(",")}


@pytest.fixture
def rendered(tmp_path: Path) -> dict[str, str]:
    if shutil.which("sh") is None or shutil.which("awk") is None:
        pytest.skip("需要 POSIX sh 與 awk（開發環境為 WSL2 / Linux）")

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = _render(out_dir)
    assert result.returncode == 0, f"render.sh 失敗：{result.stderr.decode(errors='replace')}"

    return {path.name: path.read_text(encoding="utf-8") for path in out_dir.iterdir()}


# ── A. 字面取代 ────────────────────────────────────────────────


def test_password_is_written_verbatim(rendered: dict[str, str]) -> None:
    """密碼原封不動出現在兩個檔案裡——任何字元都不得被解讀或改寫。"""
    for name in ("pgbouncer.ini", "userlist.txt"):
        assert NASTY_PASSWORD in rendered[name], f"{name} 內的密碼被改寫了"


def test_admin_password_is_written_verbatim(rendered: dict[str, str]) -> None:
    """管理帳號的密碼同樣走字面取代（只出現在 userlist.txt；ini 不需要它）。"""
    assert NASTY_ADMIN_PASSWORD in rendered["userlist.txt"], "userlist.txt 內的管理密碼被改寫了"


def test_no_placeholder_remains(rendered: dict[str, str]) -> None:
    """佔位符全部被取代——漏一個會讓 pgbouncer 以字面值當帳號密碼去連線。"""
    placeholders = (
        "__DB_USER__",
        "__DB_PASSWORD__",
        "__DB_NAME__",
        "__PGBOUNCER_ADMIN_USER__",
        "__PGBOUNCER_ADMIN_PASSWORD__",
    )
    for name, content in rendered.items():
        for placeholder in placeholders:
            assert placeholder not in content, f"{name} 仍留有未取代的 {placeholder}"


@pytest.mark.parametrize("missing", ["DB_PASSWORD", "PGBOUNCER_ADMIN_PASSWORD"])
def test_missing_secret_fails_loudly(tmp_path: Path, missing: str) -> None:
    """缺任一密碼都要當場失敗，不能產出一份帳號沒有密碼的設定檔。"""
    if shutil.which("sh") is None:
        pytest.skip("需要 POSIX sh")

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = _render(out_dir, **{missing: ""})

    assert result.returncode != 0, f"缺 {missing} 卻成功渲染"
    assert not list(out_dir.iterdir()), "失敗時不應留下半成品設定檔"


# ── B. 管理帳號與應用帳號分離 ──────────────────────────────────


def test_admin_console_user_is_not_the_application_user(rendered: dict[str, str]) -> None:
    """`admin_users` 不得是應用帳號。

    在裡面的帳號能對連線池下 PAUSE / KILL / RELOAD。應用帳號的密碼會出現在
    每一個部署單元的環境變數裡，流通範圍遠大於運維憑證該有的範圍。
    """
    admin_users = _ini_users(rendered["pgbouncer.ini"], "admin_users")

    assert DB_USER not in admin_users, (
        f"應用帳號 `{DB_USER}` 出現在 admin_users——拿到應用 DB 密碼即可 PAUSE/KILL 連線池"
    )
    assert ADMIN_USER in admin_users, f"admin_users 未包含專用管理帳號 `{ADMIN_USER}`"


def test_application_user_keeps_read_only_stats_access(rendered: dict[str, str]) -> None:
    """應用帳號降為 `stats_users`：`SHOW POOLS` 仍可跑，危險指令跑不了。

    這條是上一條的配套。少了它，正確的修法（把應用帳號從 admin_users 拿掉）
    會連帶讓壓測的觀測流程失效，而那個失效要等到下次壓測才會被發現。
    """
    stats_users = _ini_users(rendered["pgbouncer.ini"], "stats_users")

    assert DB_USER in stats_users, (
        f"應用帳號 `{DB_USER}` 不在 stats_users——`SHOW POOLS` 會失敗（壓測觀測流程斷掉）"
    )


def test_owner_role_is_not_reachable_through_the_pool(rendered: dict[str, str]) -> None:
    """migration / owner 角色不得出現在連線池的認證清單裡（13 §3.1）。

    角色拆分後有三組憑證，而只有應用角色該走 PgBouncer：

    - ``CREATE DATABASE``（pytest 建 test database）無法經 transaction mode 的
      連線池——它綁定固定 dbname。
    - migration 的 ``CREATE INDEX CONCURRENTLY``、advisory lock 在 transaction
      pooling 下語意會壞掉（05 §5.5），而壞法是零星、難重現的。

    把 owner 加進 userlist 不會有任何立即症狀（連得上、跑得動 SELECT），問題要
    等到某次 migration 才爆——所以這條在拆分當下就釘住：owner 一律走直連埠。
    ``__DB_ADMIN_USER__`` 這類佔位符若出現在樣板裡，代表有人開了那條路。
    """
    for name, content in rendered.items():
        assert "__DB_ADMIN_USER__" not in content, f"{name} 引入了 owner 角色的佔位符"
        assert "__POSTGRES_SUPERUSER__" not in content, f"{name} 引入了 superuser 的佔位符"

    entries = [
        line for line in rendered["userlist.txt"].splitlines() if line.strip().startswith('"')
    ]
    assert len(entries) == 2, (
        f"userlist.txt 有 {len(entries)} 個帳號，應為 2（應用帳號 + PgBouncer 管理帳號）：{entries}"
    )


def test_userlist_contains_both_accounts_with_distinct_passwords(rendered: dict[str, str]) -> None:
    """兩個帳號各有自己的密碼。

    共用同一組密碼等於沒有分離：`admin_users` 換了名字，但持有應用密碼的人
    照樣登得進管理主控台。
    """
    userlist = rendered["userlist.txt"]

    assert f'"{DB_USER}" "{NASTY_PASSWORD}"' in userlist, "userlist.txt 缺應用帳號"
    assert f'"{ADMIN_USER}" "{NASTY_ADMIN_PASSWORD}"' in userlist, "userlist.txt 缺管理帳號"
    assert NASTY_ADMIN_PASSWORD != NASTY_PASSWORD  # 測資本身的前提

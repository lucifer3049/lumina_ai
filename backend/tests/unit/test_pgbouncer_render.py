"""驗收：PgBouncer 設定渲染對特殊字元密碼是字面取代（docker/pgbouncer/render.sh）。

這條測試存在的原因是一個實際踩到的坑：原本用
`sed "s|__DB_PASSWORD__|$DB_PASSWORD|g"` 注入密碼，而 sed 會把替換字串當運算式解讀——

- 密碼含 `&`：sed 展開成「整段匹配文字」，產出的設定帶著**錯誤密碼且完全不報錯**，
  症狀是 pgbouncer 認證失敗，指向完全錯誤的原因（會去查網路、查 PG 的 pg_hba）。
- 密碼含 `|`：`sed: bad option in substitution expression`，容器直接掛。
- awk 的 `gsub()` 有同樣的 `&` 問題，所以 render.sh 用 index/substr 做字面取代。

測試直接跑那支 shell script（sh + awk，CI 與 WSL 皆有），不經 docker——
要驗的是取代邏輯，起容器只會讓這條測試慢十倍且需要外部依賴。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RENDER_SH = REPO_ROOT / "docker" / "pgbouncer" / "render.sh"

# 三種會讓「把值塞進替換運算式」的作法出錯的字元，一次全帶上。
NASTY_PASSWORD = r"p&w|d\ss'\"x"


@pytest.fixture
def rendered(tmp_path: Path) -> dict[str, str]:
    if shutil.which("sh") is None or shutil.which("awk") is None:
        pytest.skip("需要 POSIX sh 與 awk（開發環境為 WSL2 / Linux）")

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    subprocess.run(  # noqa: S603 —— 固定指令、參數不來自外部輸入
        ["sh", str(RENDER_SH)],  # noqa: S607
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "DB_USER": "lumina",
            "DB_PASSWORD": NASTY_PASSWORD,
            "DB_NAME": "lumina",
            "TPL_DIR": str(RENDER_SH.parent),
            "OUT_DIR": str(out_dir),
        },
    )

    return {path.name: path.read_text(encoding="utf-8") for path in out_dir.iterdir()}


def test_password_is_written_verbatim(rendered: dict[str, str]) -> None:
    """密碼原封不動出現在兩個檔案裡——任何字元都不得被解讀或改寫。"""
    for name in ("pgbouncer.ini", "userlist.txt"):
        assert NASTY_PASSWORD in rendered[name], f"{name} 內的密碼被改寫了"


def test_no_placeholder_remains(rendered: dict[str, str]) -> None:
    """佔位符全部被取代——漏一個會讓 pgbouncer 以字面值當帳號密碼去連線。"""
    for name, content in rendered.items():
        for placeholder in ("__DB_USER__", "__DB_PASSWORD__", "__DB_NAME__"):
            assert placeholder not in content, f"{name} 仍留有未取代的 {placeholder}"


def test_missing_secret_fails_loudly(tmp_path: Path) -> None:
    """缺 DB_PASSWORD 時要當場失敗，不能產出一份沒有密碼的設定檔。"""
    if shutil.which("sh") is None:
        pytest.skip("需要 POSIX sh")

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = subprocess.run(  # noqa: S603
        ["sh", str(RENDER_SH)],  # noqa: S607
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "DB_USER": "lumina",
            "DB_NAME": "lumina",
            "TPL_DIR": str(RENDER_SH.parent),
            "OUT_DIR": str(out_dir),
        },
        check=False,
    )

    assert result.returncode != 0, "缺密碼卻成功渲染"
    assert not list(out_dir.iterdir()), "失敗時不應留下半成品設定檔"

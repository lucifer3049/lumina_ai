"""E2E smoke 的基礎設施：真伺服器行程 + 真租戶（13 §1.2）。

與 tests/api 那層的差別：那層用 ``ASGITransport`` 在測試行程**內**打 app；
這層起一個真的 uvicorn 子行程走 TCP。smoke 的目的是「防 AI 跨 session 開發的
迴歸盲區」——它要驗的是**部署形狀**的伺服器起得來、整條價值迴路走得通；
行程內測試驗不到前者（settings 載入、django.setup 時序、金鑰檔存在與否，
全部在 import 期就定生死）。

**設定用 ``config.settings.dev`` 而非 ``config.settings.test``**：pytest-django
的 test DB 只存在於測試行程內的交易裡，子行程看不到。smoke 走的是開發 DB 的
正式路徑——這是刻意的：它驗的就是「make up 起來的那套環境真的能服務請求」。
代價是每輪 smoke 會在開發 DB 留下一個 smoke 租戶（slug 帶隨機尾碼不互撞）；
清理留給 ``make clean``，骨架階段不做刪租戶（那個能力本身還不存在）。

前置（缺任一項 fixture 會以可讀的錯誤失敗，而不是掛在 timeout 上）：
``make up``、``make migrate``、``make gen-jwt-keys``。
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest

_STARTUP_TIMEOUT_S = 30.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# smoke 專用的 Redis 邏輯 DB（2026-08-17，1D-5）。
#
# **Celery 的工作籃就是 Redis 的一個 DB。** smoke 起自己的 worker，但 `make start` 起的
# worker 聽的是同一組佇列——兩邊搶同一個籃子，誰先撿到誰做。而 `make start` 那個讀的是
# repo 根的 `.env`（真 provider），於是「寫入端用 A 模型、查詢端用 B 模型」，
# `UNIQUE(chunk_id, model, embedding_version)` 對不上，檢索永遠回零筆。
#
# 症狀完全不指向真因：文件照樣 `ready`、API 全部 200、smoke 只說「沒有引用」。
# 1D-5 實際被咬了一次，而當時 dev 環境已經開了一整天沒人記得。
#
# 換一個 DB 就是換一個房間的籃子，兩邊再也碰不到。**15 是保留給 smoke 的**：
# `tests/conftest.py` 的 xdist worker 用 1~14，dev 與 `make start` 用預設的 0。
_SMOKE_REDIS_DB = "15"

# smoke 的子行程一律用假 provider（CLAUDE.md：LLM 測試禁止呼叫真實 API）。
#
# **這裡必須自己寫一份，不能靠 `config/settings/test.py`**：那份強制值只對 in-process
# 的測試套件有效，而 smoke 的 API 與 worker 都跑在 `config.settings.dev` 之下——那是
# 正式的設定路徑，它會照實讀 repo 根的 `.env`。
#
# 1C-5 把真金鑰寫進 `.env` 之後，smoke 就一直在打真的 Gemini（2026-08-17 於 1D-5 發現）。
# 三個代價：**smoke 會花錢**；**會因為別人的服務中斷而紅**；而最難查的是第三個——
# 兩個子行程各自解析設定的時機不同，寫入端與查詢端可能落在不同的模型上，於是 worker
# 用 A 模型寫向量、API 用 B 模型查，`UNIQUE(chunk_id, model, embedding_version)` 對不上，
# **查詢永遠回零筆**。文件照樣是 `ready`、API 全部 200、畫面上只是「答不出東西」，
# 而那正是 `services/knowledge/embedding.py` 的 `model_for` docstring 預言過的症狀。
#
# 維度一併釘住：`.env` 的真模型維度若與 `halfvec(1536)` 不同，寫入會在 Gateway 被擋下，
# 而那個紅燈指向 provider 設定，不指向 smoke 的環境。
_MOCK_AI_ENV = {
    "AI_EMBEDDING_PROVIDER": "mock",
    "AI_EMBEDDING_MODEL": "mock-embedding",
    "AI_EMBEDDING_API_KEY": "",
    "AI_EMBEDDING_DIMENSIONS": "1536",
    "AI_CHAT_PROVIDER": "mock",
    "AI_CHAT_MODEL": "mock-chat",
    "AI_CHAT_API_KEY": "",
    "AI_CHAT_FALLBACK_MODELS": "",
    # 2B-4 之後 `.env` 會指向真的 TEI 或帶著 Jina 的金鑰。釘死它的理由與上面兩條相同，
    # 只是後果更難查：**rerank 失敗是降級而不是報錯**，所以 TEI 沒開的 smoke 仍然全綠
    # ——綠的是「降級鏈有效」，而不是「rerank 正常」，兩者在 smoke 的輸出裡長得一模
    # 一樣。反過來 TEI 有開時，smoke 會安靜地把一台 GPU 拉進 E2E 的必要條件裡。
    "AI_RERANK_PROVIDER": "mock",
    "AI_RERANK_MODEL": "mock-rerank",
    "AI_RERANK_API_KEY": "",
    # 重建在重切階段會等 ETL，推不動時隔 `reindex_poll_seconds` 回來看一次（正式
    # 預設 60 秒，因為等的是以分鐘計的整條 ETL）。e2e 的文件只有幾 KB，等 60 秒
    # 純粹是讓 smoke 慢一分鐘——調成 2 秒，驗的機制完全相同。
    "REINDEX_POLL_SECONDS": "2",
}


def _dev_env() -> dict[str, str]:
    """子行程環境：改用 dev settings，並強制假 provider。

    pytest-django 依 pyproject 的 ini 把 ``DJANGO_SETTINGS_MODULE`` 設成
    ``config.settings.test`` 並寫進 ``os.environ``——子行程若原樣繼承，
    ``config/asgi.py`` 的 ``setdefault`` 不會覆蓋它，伺服器就連上 test DB
    的殘影。必須顯式蓋掉。

    **金鑰設成空字串而不是 `pop`**：`AppSettings` 的 `env_file` 直接指向 repo 根的
    `.env`，把環境變數刪掉之後 pydantic 還是會從那個檔案讀到金鑰（`config/settings/
    test.py` 已實測過）。環境變數的優先序高於 `.env`，覆寫成空字串才蓋得掉。
    """
    return {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.dev",
        # 放在 `**os.environ` 之後才蓋得掉 xdist 在父行程設的那個值。
        "REDIS_DB": _SMOKE_REDIS_DB,
        **_MOCK_AI_ENV,
    }


@pytest.fixture(scope="session")
def api_server() -> Iterator[str]:
    """啟動 uvicorn 子行程，回傳 base URL；session 結束時關閉。

    port 動態取得：smoke 常與 ``make dev``（8000）並存，寫死會互撞且症狀是
    「連上了但行為怪」——連到的是別人。
    """
    port = _free_port()
    proc = subprocess.Popen(  # noqa: S603 —— 參數全為常數，無外部輸入
        [
            sys.executable,
            "-m",
            "uvicorn",
            "config.asgi:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-config",
            "config/uvicorn_logging.json",
            "--no-access-log",
        ],
        env=_dev_env(),
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    try:
        while True:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"uvicorn 起不來（exit={proc.returncode}）。"
                    "先確認：make up、make migrate、make gen-jwt-keys 是否都跑過"
                )
            try:
                if httpx.get(f"{base_url}/openapi.json", timeout=1.0).status_code == 200:
                    break
            except httpx.TransportError:
                pass  # 還在啟動中
            if time.monotonic() > deadline:
                raise TimeoutError(f"uvicorn {_STARTUP_TIMEOUT_S}s 內未就緒")
            time.sleep(0.2)
        yield base_url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def background_worker() -> Iterator[None]:
    """啟動一個真的 Celery worker（吃 etl 與 embedding 兩條佇列）。

    與 `api_server` 同樣的理由：ETL 的**部署形狀**是「API 送訊息、另一個行程處理」，
    而在測試行程內直接呼叫 service 驗不到那條路——broker 位址錯、task 名稱不一致、
    worker 起不來（settings 或 django.setup 時序），全部只會表現成「文件永遠停在
    uploaded」。那正是這一步要擋的迴歸。

    ``--pool threads`` **必須與 Makefile 的 `WORKER_CMD` 一致**，由
    `tests/unit/test_dev_launcher.py::test_the_smoke_worker_uses_the_same_pool` 對帳。

    這裡原本是 `--pool solo`（理由是「測試環境不需要並行，少一層行程就少一種難以歸因
    的失敗」）。那個選擇讓 smoke 剛好繞開了 1B-6 起就存在的一個缺陷：預設的 prefork
    pool 建的是 daemonic 行程，而 daemonic 行程不准有子行程——抽取正是跑在子行程裡。
    `make start` 的 worker 因此完全處理不了任何上傳，而 smoke 全綠。**測試的形狀與部署
    的形狀不同時，差異本身就是盲區**，而這次差異剛好就在出事的那一項。
    """
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "config.celery_app",
            "worker",
            "--queues",
            # 三條都要吃。只吃 etl 的話，smoke 第 3 步會停在 chunked 直到逾時，而
            # 錯誤訊息指向「ETL 未完成」——實際上 ETL 早就跑完了，沒有人接手而已。
            # `reindex` 是 2B-6 加的：少了它，重建 job 永遠停在 pending 而 API 全部
            # 200，`test_reindex_flow.py` 會以逾時的形式紅（那正是它要擋的形狀）。
            "etl,embedding,reindex",
            "--pool",
            "threads",
            "--loglevel",
            "warning",
        ],
        env=_dev_env(),
    )
    try:
        if proc.poll() is not None:
            raise RuntimeError(f"celery worker 起不來（exit={proc.returncode}）")
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=15)


@dataclass(frozen=True)
class SmokeTenant:
    slug: str
    email: str
    password: str


@pytest.fixture(scope="session")
def smoke_tenant() -> SmokeTenant:
    """經 ``manage.py create_tenant`` 建立本輪 smoke 專用租戶。

    走 CLI 而非直接呼叫 Service：smoke 驗的是 CI 也走得通的那條開通路徑
    （tests/integration/test_tenant_bootstrap.py 已驗過該指令本身；這裡是
    把它當基礎設施用）。slug 帶隨機尾碼，重跑不互撞。
    """
    slug = f"smoke-{uuid.uuid4().hex[:8]}"
    email = f"owner@{slug}.example.com"
    password = secrets.token_urlsafe(18)
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            "manage.py",
            "create_tenant",
            "--name",
            f"Smoke {slug}",
            "--slug",
            slug,
            "--owner-email",
            email,
            # ``--flag=value`` 而不是分開兩個 argv：``token_urlsafe`` 產生的密碼可能
            # 以 ``-`` 開頭，argparse 會把它當成另一個旗標而以 exit 2 結束。症狀是
            # smoke 偶爾在建租戶就掛掉，重跑又好——1B-6 實際踩到一次。
            f"--owner-password={password}",
        ],
        env=_dev_env(),
        check=True,
        capture_output=True,
        text=True,
    )
    return SmokeTenant(slug=slug, email=email, password=password)

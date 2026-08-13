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


def _dev_env() -> dict[str, str]:
    """子行程環境：改用 dev settings。

    pytest-django 依 pyproject 的 ini 把 ``DJANGO_SETTINGS_MODULE`` 設成
    ``config.settings.test`` 並寫進 ``os.environ``——子行程若原樣繼承，
    ``config/asgi.py`` 的 ``setdefault`` 不會覆蓋它，伺服器就連上 test DB
    的殘影。必須顯式蓋掉。
    """
    return {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.dev"}


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
def etl_worker() -> Iterator[None]:
    """啟動一個真的 Celery worker（只吃 etl 佇列）。

    與 `api_server` 同樣的理由：ETL 的**部署形狀**是「API 送訊息、另一個行程處理」，
    而在測試行程內直接呼叫 service 驗不到那條路——broker 位址錯、task 名稱不一致、
    worker 起不來（settings 或 django.setup 時序），全部只會表現成「文件永遠停在
    uploaded」。那正是這一步要擋的迴歸。

    ``--pool solo``：worker 預設 fork 出子行程，而抽取本身又會 fork（forkserver）。
    測試環境不需要並行，少一層行程就少一種難以歸因的失敗。
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
            "etl",
            "--pool",
            "solo",
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

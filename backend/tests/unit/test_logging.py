"""驗收測試 —— structlog 結構化日誌（12 §1.1、13 §2 Phase 0）。

驗的是 `config/logging.py` 這個**單一設定點**產出的行為，不是它的內部長相：

1. 每筆事件是**一行 JSON**，且含 12 §1.1 規定的標準欄位（ts / level / logger /
   request_id / tenant_id / event）。少一個欄位不會有任何症狀——直到出事那天
   要靠 request_id 串全鏈路時才發現串不起來。
2. **stdlib logging 也走同一條路**。api/main.py 現有的 `logger.error(...)`、
   Django 的 `django.request`、uvicorn 的錯誤都是 stdlib logger；只設定 structlog
   而不橋接 stdlib 的話，log 會是「一半 JSON 一半純文字」，Loki 那頭只解析得了一半。
3. **遮罩**（10 §5、鐵則 9）：secrets 與 PII 不得原樣落地。這條沒有測試就等於沒有，
   因為漏遮的症狀是「log 看起來很正常」。

全部在 unit 層：不碰 DB、不起服務，只呼叫 configure_logging() 後打 log 讀 stdout。
"""

from __future__ import annotations

import io
import json
import logging
import sys
import timeit
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from pydantic import SecretStr

from config.logging import (
    _mask_text,
    bind_request_context,
    clear_request_context,
    configure_logging,
    get_logger,
)

# backend/tests/unit/test_logging.py → backend/ → repo 根
_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _isolated_logging() -> Iterator[None]:
    """每條測試自行 configure_logging()（參數不同），此處只負責收尾清 context。

    context 不清會跨測試殘留：contextvars 綁的值活在 pytest 的執行 context 上，
    下一條測試會看到上一條的 request_id，斷言結果變成看執行順序而定。
    """
    yield
    clear_request_context()


def _log_lines(captured: str) -> list[dict[str, Any]]:
    """把 stdout 解析成事件清單；順帶釘住「一筆事件 = 一行」。

    多行輸出（例如 traceback 原樣印出）在 Loki 會被切成數筆無關聯的 log，
    正是結構化日誌要消除的問題，所以這裡不做寬容處理。
    """
    lines = [line for line in captured.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


class TestStandardFields:
    """12 §1.1 的標準欄位集合。"""

    def test_event_is_single_line_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", fmt="json")
        get_logger("test").info("document_ingested", doc_id="d-1")

        events = _log_lines(capsys.readouterr().out)

        assert len(events) == 1
        assert events[0]["event"] == "document_ingested"
        assert events[0]["doc_id"] == "d-1"

    def test_standard_fields_present(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", fmt="json")
        get_logger("services.knowledge").warning("quota_near_limit")

        event = _log_lines(capsys.readouterr().out)[0]

        assert event["level"] == "warning"
        assert event["logger"] == "services.knowledge"
        assert event["event"] == "quota_near_limit"
        assert event["ts"].endswith("Z"), "ts 必須是 UTC ISO-8601，帶時區才能跨機器比對"

    def test_level_below_threshold_is_dropped(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="WARNING", fmt="json")
        get_logger("test").info("noise")
        get_logger("test").error("signal")

        events = _log_lines(capsys.readouterr().out)

        assert [e["event"] for e in events] == ["signal"]

    def test_console_format_is_not_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        """dev 用人類可讀格式；json 只在容器裡有意義（12 §1.1：JSON stdout → Loki）。"""
        configure_logging(level="INFO", fmt="console")
        get_logger("test").info("hello_dev")

        out = capsys.readouterr().out

        assert "hello_dev" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out.splitlines()[0])


def _makefile_variable(makefile: str, name: str) -> str:
    """取 Makefile 變數的定義（含以反斜線續行的部分）。

    以空行為界：本 repo 的變數定義都是一段連續的行，後面接空行。用 ``\\n\\n`` 切，
    比逐行解析續行符簡單，且定義中間多一個空行時會立刻被測試發現（而不是靜默截斷）。
    """
    body = makefile.split(f"\n{name} =", 1)[1]
    return body[: body.index("\n\n")]


class TestUvicornLogConfig:
    """uvicorn 的 logger 也必須流進同一條 pipeline。

    uvicorn 預設給 ``uvicorn`` / ``uvicorn.error`` / ``uvicorn.access`` 各掛一個
    handler 且 ``propagate=False``——結果是啟動訊息與框架錯誤走純文字、應用日誌走
    JSON。這種混合輸出在 Loki 只會解析成功一半，而且「有 log」的表象讓人不會察覺。
    """

    def test_uvicorn_loggers_propagate_to_root(self) -> None:
        config = json.loads((_CONFIG_DIR / "uvicorn_logging.json").read_text(encoding="utf-8"))

        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logger_config = config["loggers"][name]
            assert logger_config["handlers"] == [], f"{name} 仍自帶 handler，輸出不會是 JSON"
            assert logger_config["propagate"] is True, f"{name} 未 propagate，root handler 收不到"

    @staticmethod
    def _launch_command(makefile: str, target: str) -> str:
        """取出 target 的啟動指令，並把 ``$(API_ARGS)`` 展開。

        不直接對 recipe 字串斷言：``api`` / ``api-pinned`` 共用一份參數定義
        （Makefile 註解說明了理由），寫死在 recipe 裡的斷言會在任何一次合理重構
        後假紅燈。``dev`` 的參數不同（單 worker + --reload），它是直接寫在 recipe
        裡的，所以這裡只在有引用時才展開。
        """
        recipe = makefile.split(f"\n{target}:", 1)[1].split("\n\n", 1)[0]
        # 變數展開一層。``dev`` 的指令在 1B-6 之前直接寫在 recipe 裡，之後搬進
        # ``DEV_CMD``（`make start` 的背景啟動要用同一份）——只讀 recipe 的話，
        # 這裡看到的是字面上的 ``$(DEV_CMD)``，於是「dev 帶了 --reload 嗎」這類
        # 斷言會在指令完全正確的情況下紅燈。
        for variable in ("API_ARGS", "DEV_CMD"):
            token = f"$({variable})"
            if token in recipe:
                recipe = recipe.replace(token, _makefile_variable(makefile, variable))
        return recipe

    @pytest.mark.parametrize("target", ["api", "api-pinned", "dev"])
    def test_uvicorn_targets_use_the_log_config(self, target: str) -> None:
        """設定檔存在但沒被啟動指令帶上等於沒有——只有跑起來才看得出差異。

        三個啟動 uvicorn 的目標都要驗。只驗一個的話，其餘漏帶 ``--log-config``
        時症狀是「那個情境的日誌格式不一樣」而不是錯誤——`dev` 尤其危險：
        開發時看到的格式與壓測 / 部署不同，問題到後面才浮現。
        """
        makefile = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        command = self._launch_command(makefile, target)

        assert "--log-config config/uvicorn_logging.json" in command
        # uvicorn 的存取日誌與 api/main.py 的 middleware 重複，且不帶 request_id。
        assert "--no-access-log" in command

    def test_no_target_enables_a_removed_feature_flag(self) -> None:
        """Makefile 不得再提到 ``ENABLE_SPIKE_ENDPOINTS``（1A-5 已刪除該旗標）。

        前一版這裡驗的是「``dev`` 不得開啟 spike 面」，因為 `api`（壓測目標）需要
        它而開發伺服器不需要。旗標整個消失之後，剩下的風險換成另一種：Makefile 留
        著一個指向不存在設定的環境變數前綴。那不會報錯——它只是無聲地沒有作用，
        然後誤導下一個讀 Makefile 的人以為那個開關還在。
        """
        makefile = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")

        assert "ENABLE_SPIKE_ENDPOINTS" not in makefile

    def test_dev_reload_dirs_cover_every_source_package(self) -> None:
        """``DEV_RELOAD_DIRS`` 必須涵蓋所有頂層原始碼套件。

        漏掉一個目錄的症狀是「改那個目錄的檔案不會重啟」——沒有錯誤訊息，只是
        改了沒反應，很容易被當成程式沒生效而白花時間。

        不能改回「監看整個 backend/」：那底下 98.5% 的檔案在 .venv/ 裡，而 WSL2
        的 DrvFs 不支援 inotify、必須用輪詢，逐一 stat 一萬多個檔案在 9p 上慢到
        形同沒有監看（Makefile 註解有實測數據）。所以是白名單 + 這條對帳測試。
        """
        makefile = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        declared = set(makefile.split("\nDEV_RELOAD_DIRS =", 1)[1].split("\n", 1)[0].split())

        backend = _CONFIG_DIR.parent
        # 排除：測試與壓測腳本（改它們不需要重啟伺服器）、建置腳本（scripts/ 是
        # `make openapi` 這類一次性工具，不在伺服器的 import 圖裡）、venv、快取。
        ignored = {
            "tests",
            "loadtest",
            "scripts",
            ".venv",
            "__pycache__",
            ".ruff_cache",
            ".pytest_cache",
        }
        packages = {
            path.name
            for path in backend.iterdir()
            if path.is_dir() and not path.name.startswith(".") and path.name not in ignored
        }

        assert packages <= declared, f"新套件未加進 DEV_RELOAD_DIRS：{sorted(packages - declared)}"

    def test_dev_target_is_single_worker_with_reload(self) -> None:
        """``dev`` 必須是單 worker + ``--reload``。

        uvicorn 的 ``--reload`` 與 ``--workers > 1`` 互斥；更關鍵的是多行程下
        中斷點落在 fork 出去的子行程、IDE 接不到。這條把「開發用單 worker」
        釘住，避免有人為了「跟 api 一致」把 workers 加回來。
        """
        command = self._launch_command((_REPO_ROOT / "Makefile").read_text(encoding="utf-8"), "dev")

        assert "--reload" in command
        assert "--workers" not in command, "dev 帶了 --workers → 與 --reload 互斥且斷點會失效"


class TestDeployedImageCommand:
    """部署用的 image 必須帶齊與 Makefile 相同的啟動旗標。

    Makefile 的三個 target 都被上方 ``TestUvicornLogConfig`` 釘住了，但**真正上線
    跑的是 backend/Dockerfile 的 CMD**——那條指令原本三項全缺（settings module、
    log-config、no-access-log），而 CI 照樣 build、scan、通過。本機與 CI 都看不出
    差異，只有部署環境的日誌會壞。
    """

    @staticmethod
    def _image_cmd() -> str:
        """取出 CMD 並把續行接起來（CMD 是多行 JSON 陣列，逐行比對會漏）。"""
        dockerfile = (_REPO_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
        joined = dockerfile.replace("\\\n", " ")
        return joined.split("\nCMD ", 1)[1].split("\n", 1)[0]

    def test_image_command_uses_the_log_config(self) -> None:
        cmd = self._image_cmd()

        assert "config/uvicorn_logging.json" in cmd, "image 未帶 --log-config → 輸出一半純文字"
        assert "--no-access-log" in cmd, "image 未帶 --no-access-log → 每個請求兩筆存取記錄"

    def test_image_pins_the_settings_module(self) -> None:
        """不指定 DJANGO_SETTINGS_MODULE 等於用 dev 設定跑部署。

        ``config/asgi.py`` 用的是 ``setdefault(..., "config.settings.dev")``，所以
        漏設不會有錯誤——dev 目前恰好等同 base，行為一致、測試全綠。危險在於 dev.py
        是「可以放開發便利設定」的地方，一旦有人在那裡加東西就會同時進到部署環境。
        """
        dockerfile = (_REPO_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")

        assert "DJANGO_SETTINGS_MODULE=config.settings.prod" in dockerfile
        assert (_CONFIG_DIR / "settings" / "prod.py").exists(), "指定了不存在的 settings module"


class TestStdlibBridge:
    """stdlib logging 必須流進同一個 pipeline。"""

    def test_stdlib_logger_emits_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", fmt="json")
        logging.getLogger("api.main").error("未預期例外 | request_id=%s", "abc")

        event = _log_lines(capsys.readouterr().out)[0]

        assert event["logger"] == "api.main"
        assert event["level"] == "error"
        assert "abc" in event["event"], "%-style 參數必須先展開，否則 log 讀不出實際值"

    def test_exception_is_rendered_inline(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", fmt="json")
        try:
            raise ValueError("boom")
        except ValueError as exc:
            logging.getLogger("api.main").error("unhandled", exc_info=exc)

        events = _log_lines(capsys.readouterr().out)

        assert len(events) == 1, "traceback 不得換行成多筆事件"
        assert "ValueError: boom" in events[0]["exception"]

    def test_stdout_is_resolved_at_emit_time(self) -> None:
        """設定之後才被替換掉的 stdout 也要收得到 log。

        configure_logging() 跑在 import 期（config/asgi.py）；handler 若在建構當下
        就把 sys.stdout 記死，之後任何重導（gunicorn/celery、redirect_stdout、
        pytest capture）都會讓日誌寫進舊物件。舊物件已關閉時 logging 會吞掉
        ValueError，症狀是**日誌整批消失且沒有錯誤訊息**。
        """
        configure_logging(level="INFO", fmt="json")
        replaced = io.StringIO()
        original = sys.stdout
        sys.stdout = replaced
        try:
            get_logger("test").info("after_swap")
        finally:
            sys.stdout = original

        assert json.loads(replaced.getvalue())["event"] == "after_swap"

    def test_configure_is_idempotent(self, capsys: pytest.CaptureFixture[str]) -> None:
        """重複設定不得疊加 handler。

        重複掛 handler 的症狀是每筆 log 出現兩次——量翻倍、成本翻倍，但沒有錯誤訊息。
        configure_logging() 會在 asgi 入口、Celery worker、測試三處被呼叫，會撞上。
        """
        configure_logging(level="INFO", fmt="json")
        configure_logging(level="INFO", fmt="json")
        logging.getLogger("api.main").info("once")

        assert len(_log_lines(capsys.readouterr().out)) == 1


class TestRequestContext:
    """request_id / tenant_id 由 contextvars 帶入，呼叫端不必逐筆傳。"""

    def test_bound_context_appears_in_every_event(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", fmt="json")
        bind_request_context(request_id="r-1", tenant_id="t-1")
        get_logger("services.chat").info("stream_started")

        event = _log_lines(capsys.readouterr().out)[0]

        assert event["request_id"] == "r-1"
        assert event["tenant_id"] == "t-1"

    def test_stdlib_logger_also_gets_context(self, capsys: pytest.CaptureFixture[str]) -> None:
        """api/main.py 的錯誤 log 走 stdlib，沒有 context 就失去關聯能力。"""
        configure_logging(level="INFO", fmt="json")
        bind_request_context(request_id="r-2", tenant_id="t-2")
        logging.getLogger("api.main").error("boom")

        assert _log_lines(capsys.readouterr().out)[0]["request_id"] == "r-2"

    def test_clear_removes_context(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", fmt="json")
        bind_request_context(request_id="r-3", tenant_id="t-3")
        clear_request_context()
        get_logger("test").info("after_clear")

        event = _log_lines(capsys.readouterr().out)[0]

        assert "request_id" not in event
        assert "tenant_id" not in event


class TestRedaction:
    """鐵則 9 / 10 §5：secrets 與 PII 不進 log。"""

    def test_secret_like_keys_are_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", fmt="json")
        get_logger("test").info(
            "provider_call",
            api_key="sk-live-1234567890",
            password="hunter2",
            authorization="Bearer abc.def",
            model="gpt-4o-mini",
        )

        event = _log_lines(capsys.readouterr().out)[0]

        assert event["api_key"] == "***"
        assert event["password"] == "***"
        assert event["authorization"] == "***"
        assert event["model"] == "gpt-4o-mini", "非敏感欄位不得被誤遮，否則 log 失去用處"

    def test_usage_counters_are_not_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        """用量計數不是秘密——它是 2A 計費的原料（06 §4）。

        ``token`` 在敏感 key 清單裡而比對是子字串的，所以 ``prompt_tokens`` 會整個
        被遮成 ``***``。那個欄位因此永遠印不出數字，而它存在的唯一理由就是那個數字；
        更糟的是它看起來像有在記錄，於是沒有人會發現用量其實查不到。
        """
        configure_logging(level="INFO", fmt="json")
        get_logger("test").info(
            "embedding_completed",
            prompt_tokens=1234,
            total_tokens=1234,
            token_count=88,
            access_token="sk-live-should-be-masked",
        )

        event = _log_lines(capsys.readouterr().out)[0]

        assert event["prompt_tokens"] == 1234
        assert event["total_tokens"] == 1234
        assert event["token_count"] == 88
        # 例外清單是逐項列舉的——不在清單上的 token 欄位仍然要遮。
        assert event["access_token"] == "***"

    def test_nested_values_are_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        """設定物件常整包被丟進 log，敏感值藏在第二層。"""
        configure_logging(level="INFO", fmt="json")
        get_logger("test").info("settings_loaded", config={"db": {"password": "hunter2"}})

        event = _log_lines(capsys.readouterr().out)[0]

        assert event["config"]["db"]["password"] == "***"

    def test_secretstr_is_never_rendered(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", fmt="json")
        get_logger("test").info("startup", redis_password=SecretStr("hunter2"))

        assert "hunter2" not in capsys.readouterr().out

    def test_key_value_pairs_in_text_are_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        """URL / 連線字串裡的 `token=...` 明文。

        第三方套件（httpx 會印出完整外呼 URL）不受我們的欄位規則管轄，只能靠
        字串樣式攔下來——AI Gateway 呼叫 provider 時 URL 帶 api_key 是常態。
        """
        configure_logging(level="INFO", fmt="json")
        logging.getLogger("httpx").info(
            "HTTP Request: GET https://api.example.com/v1/x?api_key=sk-live-abc&limit=20"
        )

        event = _log_lines(capsys.readouterr().out)[0]["event"]

        assert "sk-live-abc" not in event
        assert "limit=20" in event, "非敏感參數不該被一併吃掉"

    def test_email_is_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", fmt="json")
        get_logger("test").info("login_failed", detail="帳號 alice@example.com 密碼錯誤")

        detail = _log_lines(capsys.readouterr().out)[0]["detail"]

        assert "alice@example.com" not in detail
        assert "example.com" not in detail

    def test_national_id_is_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", fmt="json")
        get_logger("test").info("pii_found", chunk="聯絡人 A123456789")

        assert "A123456789" not in _log_lines(capsys.readouterr().out)[0]["chunk"]

    def test_long_digit_run_is_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        """卡號長度（13–19 位）的連續數字一律遮，寧可誤傷也不留卡號在 log。"""
        configure_logging(level="INFO", fmt="json")
        get_logger("test").info("payment", note="卡號 4111111111111111 已失效")

        assert "4111111111111111" not in _log_lines(capsys.readouterr().out)[0]["note"]

    def test_event_name_is_also_scanned(self, capsys: pytest.CaptureFixture[str]) -> None:
        """PII 最常出現在訊息本文，不是 kwargs。"""
        configure_logging(level="INFO", fmt="json")
        logging.getLogger("api.main").error("寄信失敗: bob@example.com")

        assert "bob@example.com" not in _log_lines(capsys.readouterr().out)[0]["event"]

    def test_json_shaped_secret_in_text_is_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        """字串化的 dict —— 複合鍵名 ＋ 帶引號的值。

        兩個都是舊樣式漏接的原因：``_`` 是 word char，所以 ``\\btoken`` 在
        ``access_token`` 上不成立；而 key 右側是 ``"`` 而不是 ``[=:]``。第三方套件
        把回應 body 塞進錯誤訊息是常態，AI Gateway 外呼失敗時就是這個形狀。
        """
        configure_logging(level="INFO", fmt="json")
        logging.getLogger("httpx").warning(
            'provider 400: {"access_token": "sk-live-abc123", "model": "gpt-4o-mini"}'
        )

        event = _log_lines(capsys.readouterr().out)[0]["event"]

        assert "sk-live-abc123" not in event
        assert "gpt-4o-mini" in event, "非敏感欄位不該被一併吃掉"

    def test_bearer_token_in_text_is_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``Authorization: Bearer <jwt>``。

        舊樣式的值從 ``Bearer`` 起算並在空白處截斷，結果是遮掉 "Bearer" 這個字、
        JWT 原封不動留著——看起來有遮，實際洩漏。
        """
        configure_logging(level="INFO", fmt="json")
        logging.getLogger("httpx").info(
            "retrying with Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"
        )

        event = _log_lines(capsys.readouterr().out)[0]["event"]

        assert "eyJhbGciOiJIUzI1NiJ9" not in event
        assert "sig" not in event.split("Authorization")[-1]

    def test_url_embedded_credentials_are_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``scheme://user:password@host`` —— AppSettings 組出的 Redis DSN 形狀。

        ``.get_secret_value()`` 的結果是普通 str，SecretStr 的自動遮罩到這裡幫不上
        忙；連線失敗時把 DSN 印進錯誤訊息是套件與我們自己都會做的事。
        """
        configure_logging(level="INFO", fmt="json")
        logging.getLogger("redis").error("connect failed: redis://:hunter2@127.0.0.1:16379/0")

        event = _log_lines(capsys.readouterr().out)[0]["event"]

        assert "hunter2" not in event
        assert "16379" in event, "主機與埠仍須看得見，否則失去診斷價值"

    def test_namedtuple_value_does_not_break_the_processor(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """namedtuple 值不得讓遮罩 processor 拋例外。

        ``type(value)(masked_list)`` 對 namedtuple 是 ``TypeError``（建構子吃位置
        參數），而 processor 的例外會從 ``logger.info()`` 呼叫點冒出去——一筆純診斷
        用的 log 把整個請求變成 500。``SplitResult`` / ``sys.version_info`` /
        ``values_list(named=True)`` 的每一列都是 namedtuple。
        """
        configure_logging(level="INFO", fmt="json")
        get_logger("test").info("outbound_call", target=urlsplit("https://api.example.com/v1"))

        event = _log_lines(capsys.readouterr().out)[0]

        assert event["event"] == "outbound_call"
        assert "api.example.com" in str(event["target"])

    def test_redaction_cost_does_not_explode_on_long_opaque_strings(self) -> None:
        """遮罩成本不得隨字串長度爆炸——**這是一條效能回歸測試**。

        踩過的坑：為了讓 ``access_token`` 這種複合鍵名命中，key 樣式曾寫成
        ``[A-Za-z0-9_.\\-]*(?:token|...)``。開頭那個無錨點的貪婪量詞使引擎在每個
        位置都先吞掉整段 word char 再回溯比對 10 個候選字，成本隨長度呈平方成長。

        為什麼這條特別值得一個測試：遮罩跑在**每一筆** log 上，而存取日誌固定帶
        ``request_id``（32 hex）與 ``tenant_id``（36 字）——兩個長且不含敏感字的
        字串，正是最壞情況。實測單筆事件的 key/value 掃描由 10.8 µs 惡化到
        158.5 µs，200 併發壓測的伺服器端 p95 從 351ms 掉到 495–611ms。而功能面
        **完全正常**：所有遮罩測試照樣全綠，只有壓測數字會變差。

        比的是同一台機器上「長字串 vs 短字串」的**相對**成本，不是絕對微秒數：
        絕對值隨硬體與 CI 負載變，相對值才是「有沒有回溯爆炸」的訊號。
        實測 ratio——修好的樣式 17.8（68/3 ≈ 22.7，即次線性）、壞樣式 133.6；
        門檻取 50，兩邊各留約 2.7 倍餘裕。
        """
        short = "GET"
        # 68 字、不含任何敏感字：最壞情況是「掃了半天什麼都沒找到」。
        long_opaque = uuid.uuid4().hex + str(uuid.uuid4())

        elapsed_short = timeit.timeit(lambda: _mask_text(short), number=3000)
        elapsed_long = timeit.timeit(lambda: _mask_text(long_opaque), number=3000)
        ratio = elapsed_long / elapsed_short

        assert ratio < 50, (
            f"長字串的遮罩成本是短字串的 {ratio:.0f} 倍——樣式出現回溯爆炸。"
            "檢查 config/logging.py 的 _VALUE_PATTERNS 是否有樣式以無錨點的貪婪量詞開頭"
        )

    def test_set_values_are_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        """set / frozenset 原本整支落到「不處理」的分支，內容完全不經遮罩。"""
        configure_logging(level="INFO", fmt="json")
        get_logger("test").info("pii_found", chunks={"聯絡人 A123456789"})

        assert "A123456789" not in capsys.readouterr().out

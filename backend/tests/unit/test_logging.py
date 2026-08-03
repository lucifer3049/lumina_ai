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
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from config.logging import (
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

    def test_make_api_uses_the_log_config(self) -> None:
        """設定檔存在但沒被啟動指令帶上等於沒有——只有跑起來才看得出差異。"""
        makefile = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        api_target = makefile.split("\napi:", 1)[1].split("\n\n", 1)[0]

        assert "--log-config config/uvicorn_logging.json" in api_target
        # uvicorn 的存取日誌與 api/main.py 的 middleware 重複，且不帶 request_id。
        assert "--no-access-log" in api_target


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

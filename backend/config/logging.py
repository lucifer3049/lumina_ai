"""structlog 設定 —— 全 repo 日誌的唯一入口（02 §2、12 §1.1）。

輸出契約（12 §1.1）：**一筆事件 = 一行 JSON**，標準欄位 ts / level / logger /
request_id / tenant_id / event。`trace_id` 與 `user_id` 由後續工作包補上
（OTel 屬 3E、認證屬 1A），此處不先留空欄位——空欄位會讓查詢誤以為值一定存在。

三個設計決定與其理由：

1. **stdlib logging 一併接管**。專案裡真正在打 log 的多半不是我們的程式碼：
   Django 的 ``django.request``、uvicorn、第三方套件全走 stdlib logger。只設定
   structlog 會得到「一半 JSON、一半純文字」的輸出，Loki 那端只解析得了一半，
   而且不會有人發現——純文字那半看起來仍然「有 log」。因此 stdlib 走
   ``ProcessorFormatter``，與 structlog 共用同一條 processor chain。

2. **遮罩是 processor，不是呼叫端的責任**（10 §5、鐵則 9）。靠每個呼叫端記得
   遮罩必然會漏，而漏掉的症狀是「log 看起來很正常」。掃描範圍涵蓋 kwargs **與
   event 本文**：``logger.error("寄信給 %s 失敗", email)`` 這種寫法會把 PII 塞進
   本文，欄位名幫不上忙。代價是誤傷（正常的長數字被遮），這是刻意接受的取捨。

3. **``LOGGING_CONFIG = None``**（見 settings/base.py）：Django 不再自行設定
   logging，改由本檔的 :func:`configure_logging` 單點負責，避免兩套設定互相覆蓋
   時「哪一份生效」要靠讀原始碼推理。呼叫點：``config/asgi.py``（服務）與
   ``manage.py``（CLI）。

uvicorn 以 CLI 啟動時會套用自己的 ``log_config``，且 access log 走它自己的格式，
與本檔的契約衝突。因此**每一條啟動指令**都必須帶 ``--log-config
config/uvicorn_logging.json --no-access-log``：Makefile 的 dev / api / api-pinned
與 backend/Dockerfile 的 CMD。缺了不會有錯誤，只會得到「一半 JSON、一半純文字」
加上每個請求兩筆欄位不同的存取記錄。由 tests/unit/test_logging.py 對帳所有啟動點。
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any, cast

import structlog

from core.tenant import try_get_current_tenant_id

_HANDLER_MARK = "_lumina_logging_handler"

REDACTED = "***"

# key 名比對用（小寫、子字串比對）。刻意不放 "auth"——那會連 "author" 一起遮掉。
# 新增項目一律經 re.escape 進正則（見 _KEY_VALUE_PAIR）：含 `-` 或 `.` 的項目
# （"x-api-key"、"client.secret"）未轉義時會變成 metachar，樣式看起來有寫、
# 實際比對的是別的東西。
_SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "private_key",
)

# **例外清單**：這些 key 含上面的子字串，但它們是「數量」而不是「秘密」。
#
# 用量計數是 2A 計費與成本控制的原料（06 §4 要求每次呼叫回報 token 數）。遮掉之後
# log 裡是一個永遠印成 ``***`` 的欄位——它不保護任何東西，只讓讀 log 的人以為用量是
# 機密，而真正要查「這個租戶為什麼這麼貴」時那一欄毫無用處。
#
# **逐項列舉而不是寫規則**（例如「以 _tokens 結尾就放行」）：規則會在下一個
# ``refresh_tokens`` / ``token_secret`` 這種欄位上失效，而它失效的方向是**洩漏**。
# 清單漏列的方向則只是多遮一個數字——兩種錯誤的代價差好幾個數量級。
#
# 只作用於 **key 名比對**。字串值裡的 ``tokens=123`` 仍會被 `_KEY_VALUE_PAIR` 遮掉，
# 那是刻意的：自由文字裡分不出那是我們的計數還是第三方印出來的憑證。
_NON_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "token_count",
        "max_tokens",
    }
)

# 值樣式比對。前後的 lookaround 是必要的：沒有它，request_id（32 位十六進位）
# 這類混合字串裡的數字片段會被當成卡號遮掉，追查時反而失去關聯能力。
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_TW_ID = re.compile(r"(?<![0-9A-Za-z])[A-Z][12]\d{8}(?![0-9A-Za-z])")
_TW_PHONE = re.compile(r"(?<![0-9A-Za-z])09\d{2}-?\d{3}-?\d{3}(?![0-9A-Za-z])")
_DIGIT_RUN = re.compile(r"(?<![0-9A-Za-z])\d{13,19}(?![0-9A-Za-z])")

# URL 內嵌憑證 `scheme://user:password@host`。AppSettings 組出來的 Redis DSN
# 正是這個形狀（``redis://:{password}@host``），而 ``.get_secret_value()`` 的結果
# 是普通 str——SecretStr 的自動遮罩（見 _mask_value）到這裡已經幫不上忙。
# 連線失敗時把 DSN 印進錯誤訊息是套件與我們自己都會做的事。
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^\s/:@]*):([^\s/@]*)@")

# `token=xxx` / `api_key=xxx` 形式（query string、連線字串、第三方套件印出的 URL）。
# 我們自己的存取日誌會先砍掉 query string，但**第三方 logger 不會**：httpx 的
# "HTTP Request: GET https://...?api_key=..." 就是一整條明文，而那是 AI Gateway
# 外呼時真實會發生的情況。key 名比對用同一份 _SENSITIVE_KEY_PARTS。
#
# 三個看似多餘的細節，各對應一種實際漏接（tests/unit/test_logging.py 逐條釘住）：
#
# 1. **沒有 ``\b``**：``_`` 是 word char，所以 ``\btoken`` 在 ``access_token`` 上
#    **不成立**——而 JSON 化的 payload 幾乎都是這種複合鍵名。不加邊界即可命中
#    內部；未被捕獲的前綴（``access_``）原樣留在字串裡，輸出不受影響。
# 2. 分隔符與值各允許一個引號：``{"access_token": "sk-..."}`` 的 key 右側是 `"`
#    而不是 `[=:]`，值左側同理。少了這一層，字串化的 dict 整包不會被遮。
# 3. 值可帶 ``Bearer`` / ``Basic`` scheme：``Authorization: Bearer <jwt>`` 的值
#    若從 ``Bearer`` 起算就會在空白處截斷，結果是遮掉 "Bearer" 這個字、JWT 原樣
#    留著。scheme 不回填進輸出——它不是機密，但留著只會讓人以為遮罩失敗。
#
# ⚠️ **樣式不得以無錨點的貪婪量詞開頭。** 曾經為了涵蓋 key 的前綴而寫成
# ``[A-Za-z0-9_.\-]*(?:token|...)``，結果每個位置都要先吞掉整段 word char 再回溯
# 比對 10 個候選字，成本隨字串長度呈平方成長。本檔跑在**每一筆** log 上，而存取
# 日誌的 ``request_id``（32 hex）與 ``tenant_id``（36 字）正是最壞情況：單筆事件
# 的 key/value 掃描由 10.8 µs 惡化到 158.5 µs（14.7 倍），200 併發壓測的 p95 從
# 351ms 掉到 495–611ms。以敏感字本身開頭讓引擎能用字首集合快速跳過不可能的位置，
# 恢復到 17.9 µs（相對原樣式 1.7 倍，換到的是上述三個漏接）。
_SENSITIVE_KEY_ALTERNATION = "|".join(re.escape(part) for part in _SENSITIVE_KEY_PARTS)
_KEY_VALUE_PAIR = re.compile(
    rf"(?i)((?:{_SENSITIVE_KEY_ALTERNATION})[A-Za-z0-9_.\-]*)"
    rf"([\"']?\s*[=:]\s*)([\"']?)(?:bearer|basic|token)?\s*[^&\s\"',}}）]+"
)

_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # URL 憑證先掃：DSN 裡的密碼可能含 `=`，被 key/value 樣式咬掉一段後
    # 剩下的半截仍是明碼。
    (_URL_CREDENTIALS, rf"\1\2:{REDACTED}@"),
    (_KEY_VALUE_PAIR, rf"\1\2\3{REDACTED}"),
    # email 先掃：email 內含的數字可能先被其他樣式咬掉一段，留下半截明碼。
    (_EMAIL, "<email>"),
    (_TW_ID, "<id>"),
    (_TW_PHONE, "<phone>"),
    (_DIGIT_RUN, "<redacted>"),
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _NON_SENSITIVE_KEYS:
        return False
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _mask_text(text: str) -> str:
    for pattern, replacement in _VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _mask_value(value: Any) -> Any:
    """遞迴遮罩。設定物件常整包被丟進 log，敏感值藏在第二、三層。"""
    # SecretStr / SecretBytes 以 duck typing 判斷，避免這個底層模組相依 pydantic。
    if hasattr(value, "get_secret_value"):
        return REDACTED
    if isinstance(value, str):
        return _mask_text(value)
    if isinstance(value, dict):
        return {
            key: (REDACTED if _is_sensitive_key(str(key)) else _mask_value(item))
            for key, item in value.items()
        }
    if isinstance(value, (set, frozenset)):
        # 沒有這一支的話 set 會落到最後的 return value，內容完全不經遮罩。
        return type(value)(_mask_value(item) for item in value)
    if isinstance(value, (list, tuple)):
        masked = [_mask_value(item) for item in value]
        if not isinstance(value, tuple):
            return masked
        # namedtuple 的建構子吃**位置參數**，``type(value)(list)`` 會
        # ``TypeError: __new__() missing N required positional arguments``——而這裡
        # 位於 processor chain 內，例外會從 logger.info() 呼叫點冒出去，把一筆純
        # 診斷用的 log 變成 500。urlsplit 的 SplitResult、sys.version_info、
        # values_list(named=True) 的每一列都是 namedtuple。
        make = getattr(value, "_make", None)  # namedtuple 的公開建構 API
        return make(masked) if callable(make) else tuple(masked)
    return value


def redact_processor(
    _logger: object, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """secrets 與 PII 的最後一道關卡（10 §5、鐵則 9）。

    以底線開頭的 key 是 structlog / ProcessorFormatter 的內部欄位（``_record``
    帶的是 LogRecord 物件），不遞迴進去——那不是要輸出的內容。
    """
    for key, value in event_dict.items():
        if key.startswith("_"):
            continue
        event_dict[key] = REDACTED if _is_sensitive_key(key) else _mask_value(value)
    return event_dict


class _StdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """輸出到 stdout，且**每次 emit 才解析** ``sys.stdout``。

    ``logging.StreamHandler(sys.stdout)`` 會在建構當下把物件記下來。
    :func:`configure_logging` 跑在 import 期（config/asgi.py），任何在那之後替換
    stdout 的行為——gunicorn/celery 的輸出重導、``contextlib.redirect_stdout``、
    pytest 的 capture——都會讓 handler continue 寫進舊物件。舊物件若已關閉，
    logging 會吞掉 ``ValueError``（``Handler.handleError``），症狀是**日誌整批
    消失且沒有任何錯誤**。
    """

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self) -> Any:
        return sys.stdout

    @stream.setter
    def stream(self, value: Any) -> None:
        """吞掉 ``StreamHandler.__init__`` 的賦值；串流一律由 property 決定。"""


def tenant_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """在**寫出這一筆 log 的當下**補上 ``tenant_id``（13 §3.2、12 §1.1）。

    為什麼不是在 middleware 進入時綁一次：1A 之後租戶來自已驗證的 JWT claim，
    而那是 route 層的 ``Depends``——比所有 middleware 都晚執行。middleware 進入
    時取的快照那時還是空的，於是**每一筆 log 的 tenant_id 都會靜靜消失**，
    沒有任何錯誤，只有查詢「某個租戶的錯誤」時發現撈不到東西。

    改成 emit 時讀 contextvar 之後，不論租戶是由 middleware、route dependency
    還是背景任務設定的，都一樣抓得到。

    已經有值時不覆蓋：呼叫端顯式綁定的租戶（例如跨租戶維運任務逐一處理時）
    比 contextvar 更精確。
    """
    if event_dict.get("tenant_id") is None:
        tenant_id = try_get_current_tenant_id()
        if tenant_id is not None:
            event_dict["tenant_id"] = str(tenant_id)
    return event_dict


def _shared_processors() -> list[Any]:
    """structlog 與 stdlib 兩條路徑共用的 chain（順序有意義）。"""
    return [
        # contextvars 先合併：request_id / tenant_id 之後才進得了遮罩與 renderer。
        structlog.contextvars.merge_contextvars,
        # 合併之後、遮罩之前補租戶：這樣它同樣會經過下游所有 processor。
        tenant_processor,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.processors.StackInfoRenderer(),
        # traceback 轉成單一字串欄位 exception：換行由 JSON renderer 轉義，
        # 一筆事件仍是一行——否則 Loki 會把 traceback 切成數十筆無關聯的 log。
        structlog.processors.format_exc_info,
        # 遮罩放在 renderer 前的最後一步：確保它看得到上面所有 processor 產生的欄位
        # （尤其是 exception 字串，traceback 裡照樣可能有 PII）。
        redact_processor,
    ]


def configure_logging(*, level: str | None = None, fmt: str | None = None) -> None:
    """設定 structlog 與 stdlib logging；重複呼叫安全。

    ``level`` / ``fmt`` 皆省略時才讀 :class:`AppSettings`（鐵則 9：設定走
    Pydantic Settings）。兩者都給值時完全不碰設定檔——unit 測試因此不需要 ``.env``。

    **冪等性是必要的**，不是保險：本函式會在 asgi 入口、CLI、每條測試裡各被呼叫
    一次。重複掛 handler 的症狀是每筆 log 出現兩次——量與成本翻倍，但沒有任何
    錯誤訊息。
    """
    if level is None or fmt is None:
        from config.settings.app_settings import get_app_settings

        settings = get_app_settings()
        level = level or settings.log_level
        fmt = fmt or settings.log_format

    shared = _shared_processors()
    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if fmt == "json"
        # colors=False：日誌會被導進檔案或 Loki，ANSI escape 在那裡只是雜訊。
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # 不快取 logger：快取會讓重新設定（測試、dev 熱重載）對已取得的 logger 無效，
        # 症狀是「改了設定卻沒反應」。代價是每次 get_logger 多一次組裝，可忽略。
        cache_logger_on_first_use=False,
    )

    handler = _StdoutHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )
    setattr(handler, _HANDLER_MARK, True)

    root = logging.getLogger()
    # 只移除自己掛的：pytest、Sentry 等外部 handler 不該被這裡順手拆掉。
    for existing in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """取得結構化 logger。業務程式碼一律用這個，不要用 ``logging.getLogger``。"""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))


def bind_request_context(*, request_id: str, tenant_id: str | None = None) -> None:
    """把追蹤欄位綁進 contextvars，之後同一請求的每筆 log 自動帶上。

    走 contextvars 而非逐層傳參數：request_id 要出現在 service、repository、
    threadpool 內的每一筆 log，靠參數傳遞等於每個函式簽章都多一個欄位。
    ``sync_to_async`` 會複製 context（見 core/tenant.py），threadpool 那頭也拿得到。
    """
    structlog.contextvars.bind_contextvars(request_id=request_id)
    if tenant_id is not None:
        structlog.contextvars.bind_contextvars(tenant_id=tenant_id)


def clear_request_context() -> None:
    """清空請求層 context。**必須在請求結束時呼叫**——殘留的 tenant_id 出現在
    下一個請求的 log 上，比沒有 log 更糟：它會把追查引到錯誤的租戶。
    """
    structlog.contextvars.clear_contextvars()

"""驗收：middleware 的堆疊順序（二次架構審計 F-11 的掛載位置）。

`api/main.py` 的註解說明了每一層為什麼在那個位置，而註解擋不住任何東西——
`add_middleware` 的順序改一行不會有任何症狀，只有行為變了。

稽核那一層的順序另有 `test_audit_registry.py` 釘著（那是它自己的檔案）；這裡補的是
**頻率限制**進來之後的三個相對關係。

`user_middleware` 的**第一個是最外層**（Starlette 由後往前包）。
"""

from __future__ import annotations

from api.main import create_app
from api.middleware.body_limit import BodySizeLimitMiddleware
from api.middleware.rate_limit import RateLimitMiddleware
from api.middleware.request_context import RequestContextMiddleware


def _order() -> list[str]:
    # 比名字而不是比類別，理由同 test_audit_registry.py（Starlette 把 `Middleware.cls`
    # 標成 `_MiddlewareFactory[P]`，拿具體類別去比在 mypy strict 下不合型別）。
    return [getattr(entry.cls, "__name__", "") for entry in create_app().user_middleware]


def test_rate_limit_runs_inside_the_request_context() -> None:
    """被擋下的請求仍然要有 request_id 與一筆存取日誌。

    反過來（限流在最外層）的話，429 在日誌上完全看不見——而「使用者說一直被擋」
    是這道防線最常見的客訴，查不出是哪一層擋的等於這些 log 白記了。
    """
    names = _order()

    assert names.index(RequestContextMiddleware.__name__) < names.index(
        RateLimitMiddleware.__name__
    )


def test_rate_limit_runs_outside_the_body_size_limit() -> None:
    """被限流擋下的請求連 `Content-Length` 都不必看。

    反過來的話，一個正在被限流的來源送 100 個大請求，每一個都要先過 body 檢查
    ——而那正是限流要省下的工。
    """
    names = _order()

    assert names.index(RateLimitMiddleware.__name__) < names.index(BodySizeLimitMiddleware.__name__)


def test_every_layer_is_actually_mounted() -> None:
    """整組釘住存在性：少掛一層不會有錯誤，只會少一道防線。"""
    names = set(_order())

    for middleware in (RequestContextMiddleware, RateLimitMiddleware, BodySizeLimitMiddleware):
        assert middleware.__name__ in names, f"{middleware.__name__} 沒有掛上"

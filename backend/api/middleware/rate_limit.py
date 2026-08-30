"""HTTP 頻率限制 —— per-IP 的粗粒度擋線（09 §1.3、10 §2.1；二次架構審計 F-11＋L3）。

**它擋的不是配額。** 配額（`services/platform/quota.py`）問的是「這個租戶這一期還有
多少額度」，要先認證才知道租戶是誰；這一層問的是「這個來源在這一分鐘打了幾次」，
而它必須在認證**之前**生效——否則登入端點就完全沒有保護。兩者的鍵、時窗與處置都不同，
合併會讓其中一個失去意義。

**L3 是這一層存在的第二個理由。** `AuthService` 的登入失敗計數以 `tenant+email` 為鍵，
且每次失敗都重設 TTL（那個 docstring 自己寫著「持續攻擊會讓鎖定持續延長」）——知道
租戶 slug 與 email 的人可以**持續鎖住任何帳號**，而帳號的主人拿正確密碼也進不來。
per-IP 的擋線把「無限次嘗試」變成「每分鐘 N 次」，鎖定型 DoS 的成本因此不再是零。
它不是完整解（分散式來源仍可繞過），但那需要的是 WAF／CDN 層，不是應用程式。

**兩個桶，不是一個**：
- 認證端點（`/api/v1/auth/*`）用小額度——那裡的每一次請求都在猜密碼或換 token。
- 其餘端點用大額度——正常使用者開一個聊天頁就會打十幾次 API，把它們壓到跟登入
  一樣嚴，等於把 rate limit 變成「正常使用會壞掉」的東西。

**固定時窗（fixed window）而不是滑動窗**：`INCR` + `EXPIRE` 兩個命令、一次 round trip，
而滑動窗要維護一個有序集合。代價是窗邊界可以擠進兩倍的量（59 秒打滿、61 秒再打滿）
——對一道「把無限變成有限」的粗擋線而言，那個係數不重要。

**Redis 掛掉時放行（fail open）**，與系統其他地方的 fail closed 相反，這是刻意的：
rate limit 是保護機制不是安全邊界，讓它在 Redis 抖動時把整個網站關掉，是用一個
確定的故障換一個可能的攻擊。真正的安全邊界（認證、租戶隔離、配額）都不在這一層。
"""

from __future__ import annotations

from typing import Any, cast

from starlette.types import ASGIApp, Receive, Scope, Send

from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from core.exceptions import ErrorCode
from core.redis import get_async_redis

logger = get_logger(__name__)

__all__ = ["RateLimitMiddleware"]

# 認證端點的前綴。**用前綴而不是逐一列出**：`/auth/login`、`/auth/refresh`、
# `/auth/logout` 之外，2C 還會加 API Key 換 token——漏列一支的症狀是「那一支沒有保護」，
# 而它不會有任何徵兆。
_AUTH_PREFIX = "/api/v1/auth"

# 探測端點不限流：編排器每幾秒打一次，把它們算進同一個桶會讓 K8s 的 probe 自己
# 把節點打成 429，而那看起來像應用壞了（見 `api/health.py`）。
_EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})


class RateLimitMiddleware:
    """超過時窗額度 → 429 + `Retry-After`，不進下游。"""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        settings = get_app_settings()
        if scope["type"] != "http" or not settings.rate_limit_enabled:
            await self._app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        if path in _EXEMPT_PATHS:
            await self._app(scope, receive, send)
            return

        client_ip = _client_ip(scope, trust_proxy=settings.rate_limit_trust_proxy_headers)
        is_auth = path.startswith(_AUTH_PREFIX)
        limit = settings.rate_limit_auth_per_minute if is_auth else settings.rate_limit_per_minute
        if limit <= 0 or await self._within(
            client_ip, bucket="auth" if is_auth else "api", limit=limit
        ):
            await self._app(scope, receive, send)
            return

        logger.warning("rate_limited", client_ip=client_ip, path=path, limit=limit)
        await self._refuse(scope, receive, send)

    async def _within(self, client_ip: str, *, bucket: str, limit: int) -> bool:
        """這一分鐘還有額度嗎？Redis 不可用時一律 True（fail open，見模組 docstring）。

        **走 `get_async_redis()` 而不是同步 client**：middleware 跑在 event loop 上
        （不像 service 層有 `run_orm` 的 threadpool），同步 client 的每一次 incr 都是
        loop 上的阻塞 I/O——Redis 一抖（failover、網路抖動），每個請求都掛到 socket
        timeout（兩個指令、各 ~500ms），這個 replica 上所有 in-flight 請求與 SSE
        串流被串行化，症狀是「Redis 一抖整站凍結」。與 SSE 走的是 core/redis.py 記載
        的同一條 event-loop 例外，非阻塞那一份自帶 500ms socket_timeout。

        **key 不帶租戶前綴**，是全 repo 少數的例外：這一層跑在認證之前，租戶還不知道
        是誰。前綴改用 `rl:` 並含時窗編號——時窗換了就是新的 key，不必另外重置。
        """
        window = int(_now_seconds() // 60)
        key = f"rl:{bucket}:{client_ip}:{window}"
        try:
            client = get_async_redis()
            used = cast("int", await client.incr(key))
            if used == 1:
                # 只在建立時設一次：每次都設等於把時窗變成滑動的，而那不是這裡的語意。
                # 121 秒而不是 60：時窗結束後留一點餘裕，避免時鐘微幅偏移讓 key 早退。
                await client.expire(key, 121)
            return used <= limit
        except Exception:
            logger.warning("rate_limit_backend_unavailable", exc_info=True)
            return True

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 自己組回應而不是 raise，理由同 `body_limit.py`：例外處理器在本層**裡面**，
        # 這裡 raise 只會得到 ServerErrorMiddleware 的純文字 500。
        from starlette.requests import Request

        from api.main import problem_response
        from api.middleware.request_context import request_id_of

        retry_after = get_app_settings().rate_limit_retry_after_seconds
        response = problem_response(
            status=429,
            code=str(ErrorCode.RATE_LIMITED),
            detail="請求過於頻繁，請稍後再試",
            request_id=request_id_of(Request(scope, receive)),
            extensions={"details": {"retry_after_seconds": retry_after}},
            headers={"Retry-After": str(retry_after)},
        )
        await response(scope, receive, send)


def _now_seconds() -> float:
    import time

    return time.time()


def _client_ip(scope: Scope, *, trust_proxy: bool) -> str:
    """請求的來源位址。

    **預設不看 `X-Forwarded-For`。** 那個標頭是 client 送的，直接採信等於讓任何人
    自報一個假 IP——每個請求換一個，rate limit 就完全失效，而且它會安靜地失效
    （計數器照樣在動，只是每個 key 都只有 1）。

    只有在確定「有一個我們控制的反向代理會覆寫這個標頭」時才開
    `RATE_LIMIT_TRUST_PROXY_HEADERS`。那時取的是**最左邊**那個——代理會把真實來源
    附在最前面。（更嚴謹的做法是從右往左跳過已知的代理 IP，那需要一份代理清單，
    等 Phase 4 的反向代理真的落地再說。）

    取不到來源時回 `"unknown"` 而不是放行：那會讓所有取不到 IP 的請求共用一個桶，
    比完全不限流安全。
    """
    # `Scope` 是 `MutableMapping[str, Any]`，所以底下每一個取值都是 Any——mypy strict
    # 會擋下直接回傳，因此收斂成 str 之後才交出去（同 `core/redis.py` 的 cast 慣例）。
    if trust_proxy:
        headers: Any = scope.get("headers", [])
        for name, value in headers:
            if bytes(name).lower() == b"x-forwarded-for":
                first = bytes(value).decode("latin-1").split(",")[0].strip()
                if first:
                    return str(first)
    client: Any = scope.get("client")
    if isinstance(client, tuple | list) and client:
        host = client[0]
        return str(host) if host else "unknown"
    return "unknown"

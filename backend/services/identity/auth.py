"""AuthService —— 登入、換發、撤銷（04 §Auth、10 §2.1）。

**這一層是同步的**（ADR-001）：Django ORM 是同步的，endpoint 一律經
``core.db.run_orm`` 把呼叫送進 threadpool。Redis 也在同一條執行緒上跑，所以用
同步 client 不會阻塞 event loop。

三種撤銷手段，各自解決不同層級的問題——少任何一個都會留下一個沒有煞車的情境：

| 手段 | 粒度 | 用在什麼情況 |
|------|------|--------------|
| jti denylist | 一張 access token | 登出 |
| refresh family 撤銷 | 一次登入（一台裝置） | 偵測到 refresh 被竊取 |
| ``token_version`` +1 | 整個帳號 | 改密碼、停用帳號 |

**為什麼 refresh 要 rotation**：每次換發都作廢舊的那張，於是外洩的 refresh 只在
「下一次正常換發之前」有效。而**一張已用過的 refresh 再次出現**，代表有兩個持有
者（本人與攻擊者）——系統分不出誰是誰，所以整個家族一起撤銷，逼真正的使用者
重新登入。體驗上是負面的，但另一個選項是默認攻擊者長期在線。

**Redis key 一律經 `core.redis.tenant_key`**（鐵則 4）：漏了前綴的後果是兩個租戶
共用同一份撤銷名單或計數器，而那不會有任何錯誤訊息。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from common.passwords import hash_password, needs_rehash, verify_password
from config.settings.app_settings import get_app_settings
from core.exceptions import (
    AccountLockedError,
    InvalidCredentialsError,
    TokenRevokedError,
)
from core.redis import get_redis, tenant_key
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import RoleRepository, TenantDirectoryRepository, UserRepository
from services.identity.tokens import ACCESS_TTL, AccessClaims, TokenCodec, get_token_codec

# 密碼比對的假雜湊：帳號不存在時照樣跑一次驗證，讓兩種失敗的**耗時**也一致。
# 只比對回應內容是不夠的——不存在的帳號若快 100 毫秒回來，時間差本身就是
# 「這個 email 在不在」的答案（10 §2.1 要求 constant-time 行為）。
_DUMMY_HASH = hash_password("timing-equalizer-not-a-real-password")


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_at: datetime


class AuthService:
    def __init__(
        self,
        *,
        users: UserRepository | None = None,
        roles: RoleRepository | None = None,
        directory: TenantDirectoryRepository | None = None,
        codec: TokenCodec | None = None,
    ) -> None:
        self._users = users or UserRepository()
        self._roles = roles or RoleRepository()
        self._directory = directory or TenantDirectoryRepository()
        self._codec = codec or get_token_codec()
        self._settings = get_app_settings()

    # ── 租戶定位（登入的第一步）────────────────────────────────

    def resolve_tenant(self, slug: str) -> uuid.UUID:
        """slug → tenant_id。查不到一律當成憑證錯誤。

        回 404「查無此租戶」的話，這個端點就變成「這個平台有哪些客戶」的查詢
        工具——拿公司名稱清單掃一遍即可，而客戶名單本身就是商業情報。
        """
        tenant_id = self._directory.get_active_tenant_id(slug)
        if tenant_id is None:
            raise InvalidCredentialsError
        return tenant_id

    # ── 登入 ────────────────────────────────────────────────────

    def login(self, *, tenant_id: uuid.UUID, email: str, password: str) -> TokenPair:
        redis = get_redis()
        attempts_key = tenant_key(tenant_id, "login-fail", email)

        # redis-py 6 的同步 client 與 async client 共用型別，每個命令的回傳都是
        # ``Awaitable | Any``。這裡的 client 一定是同步的（core.redis 的單例），
        # 因此以 cast 收斂——擴散到呼叫端會讓每個運算都要先判型別。
        locked_for = cast(int, redis.ttl(attempts_key))
        if self._is_locked(attempts_key):
            raise AccountLockedError(retry_after_seconds=max(int(locked_for), 0))

        with tenant_context(tenant_id), unit_of_work():
            user = self._users.get_by_email(email)
            # 帳號不存在時仍然跑一次雜湊驗證（見 _DUMMY_HASH）。
            stored_hash = user.password_hash if user is not None else _DUMMY_HASH
            password_ok = verify_password(password, stored_hash)

            if user is None or not password_ok or user.status != "active":
                # 停用的帳號與錯誤的密碼回同一件事：告訴對方「這個帳號被停用了」
                # 等於確認帳號存在，那是帳號列舉的另一個入口。
                self._record_failure(attempts_key)
                raise InvalidCredentialsError

            if needs_rehash(user.password_hash):
                # 參數調強之後的自動升級——只有此刻手上有明文密碼。
                self._users.upgrade_password_hash(user.id, hash_password(password))

            roles = self._roles.names_for_user(user.id)
            self._users.touch_last_login(user.id)
            token_version = user.token_version

        redis.delete(attempts_key)
        return self._issue_pair(
            tenant_id=tenant_id,
            user_id=user.id,
            roles=roles,
            token_version=token_version,
            family_id=uuid.uuid4(),
        )

    # ── 換發（rotation + 竊取偵測）──────────────────────────────

    def refresh(self, refresh_token: str) -> TokenPair:
        claims = self._codec.decode_refresh(refresh_token)
        redis = get_redis()
        family_key = tenant_key(claims.tenant_id, "refresh-family", str(claims.family_id))

        current_jti = redis.get(family_key)
        if current_jti is None:
            # 家族不存在 = 已被撤銷（或早已過期）。
            raise TokenRevokedError("這個 session 已失效，請重新登入")

        if current_jti != str(claims.jti):
            # **重放**：這張 refresh 已經被換過了，卻又出現一次 → 有兩個持有者。
            # 分不出誰是本人，所以整條 session 鏈一起中止。
            redis.delete(family_key)
            raise TokenRevokedError("偵測到 refresh token 重複使用，已終止此 session")

        with tenant_context(claims.tenant_id), unit_of_work():
            user = self._users.get_by_id(claims.sub)
            if user is None or user.status != "active":
                raise TokenRevokedError("帳號已停用")
            if user.token_version != claims.token_version:
                # 改過密碼或被停用過——這張 token 屬於上一個世代。
                redis.delete(family_key)
                raise TokenRevokedError("憑證已過期，請重新登入")
            roles = self._roles.names_for_user(user.id)
            token_version = user.token_version

        return self._issue_pair(
            tenant_id=claims.tenant_id,
            user_id=claims.sub,
            roles=roles,
            token_version=token_version,
            family_id=claims.family_id,
        )

    # ── 撤銷 ────────────────────────────────────────────────────

    def logout(
        self,
        *,
        tenant_id: uuid.UUID,
        jti: uuid.UUID,
        access_expires_at: datetime,
        refresh_token: str | None = None,
    ) -> None:
        """登出：access token 進撤銷名單，並撤銷這次登入的 refresh 家族。

        少了撤銷名單，「登出」只是前端把 token 丟掉——被竊取的那一份照樣能用滿
        15 分鐘，而使用者以為自己已經登出了。

        名單的 TTL 設成 token 的剩餘壽命：過期之後它本來就無效，再留著只是佔記憶體。
        """
        redis = get_redis()
        remaining = int((access_expires_at - datetime.now(UTC)).total_seconds())
        if remaining > 0:
            redis.set(tenant_key(tenant_id, "jti-denied", str(jti)), "1", ex=remaining)

        if refresh_token is None:
            return
        try:
            refresh_claims = self._codec.decode_refresh(refresh_token)
        except Exception:
            return
        redis.delete(
            tenant_key(refresh_claims.tenant_id, "refresh-family", str(refresh_claims.family_id))
        )

    def bump_token_version(self, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """全域撤銷（DB 側）：改密碼 / 停用帳號時呼叫（10 §2.1）。

        單獨呼叫只會讓**換發**失效；要讓現有 access token 立刻失效請用
        :meth:`revoke_all_sessions`。
        """
        with tenant_context(tenant_id), unit_of_work():
            self._users.bump_token_version(user_id)

    def revoke_all_sessions(self, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """讓這個帳號**現有的 access token 立刻失效**，不是等 15 分鐘過期。

        1A-3 只把 `token_version` 寫進 DB，而每個請求的驗證不查 DB（那是熱路徑，
        一次查詢會乘上全部流量），所以拉高版本要等下次換發才生效。對「員工離職、
        當場停用」來說，那 15 分鐘的空窗不能接受，而且完全沒有徵兆。

        修法是把新版本號寫一份到 Redis，驗證時順便讀——反正那裡本來就要查一次
        撤銷名單，多讀一個 key 幾乎不花錢。TTL 設成 access token 的壽命：超過
        那個時間之後，舊 token 本來就過期了，這個標記再留著只是佔記憶體。
        """
        with tenant_context(tenant_id), unit_of_work():
            self._users.bump_token_version(user_id)
            user = self._users.get_by_id(user_id)
            minimum = user.token_version if user is not None else 1

        get_redis().set(
            tenant_key(tenant_id, "min-token-version", str(user_id)),
            str(minimum),
            ex=int(ACCESS_TTL.total_seconds()),
        )

    # ── 驗證（給 API 層的 dependency 用）──────────────────────────

    def describe_access_token(self, access_token: str) -> AccessClaims:
        """驗簽 + 檢查撤銷狀態，回傳 claims。

        撤銷檢查走 Redis 而不是資料庫：這條路徑在**每一個請求**上都會跑，一次
        DB 查詢的成本會直接乘上整體流量。代價是 `token_version` 的變更要另外
        寫進 Redis（見 :meth:`bump_token_version` 的呼叫端 1A-4 補上）。
        """
        claims = self._codec.decode(access_token)
        redis = get_redis()
        if redis.exists(tenant_key(claims.tenant_id, "jti-denied", str(claims.jti))):
            raise TokenRevokedError("憑證已登出")

        minimum = cast(
            str | None,
            redis.get(tenant_key(claims.tenant_id, "min-token-version", str(claims.sub))),
        )
        if minimum is not None and claims.token_version < int(minimum):
            # 改密碼或帳號停用之後簽發前的舊 token。
            raise TokenRevokedError("憑證已失效，請重新登入")
        return claims

    # ── 內部 ────────────────────────────────────────────────────

    def _is_locked(self, attempts_key: str) -> bool:
        raw = cast(str | None, get_redis().get(attempts_key))
        return raw is not None and int(raw) >= self._settings.login_max_attempts

    def _record_failure(self, attempts_key: str) -> None:
        """失敗計數 +1，並讓整個計數在鎖定時間之後自然消失。

        每次失敗都重設 TTL 是刻意的：持續攻擊會讓鎖定持續延長，而正常使用者
        打錯一兩次之後 15 分鐘就自動恢復，不需要人工解鎖。
        """
        pipeline = get_redis().pipeline()
        pipeline.incr(attempts_key)
        pipeline.expire(attempts_key, self._settings.login_lockout_seconds)
        pipeline.execute()

    def _issue_pair(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        roles: tuple[str, ...],
        token_version: int,
        family_id: uuid.UUID,
    ) -> TokenPair:
        access = self._codec.issue_access(
            user_id=user_id,
            tenant_id=tenant_id,
            roles=roles,
            token_version=token_version,
        )
        refresh = self._codec.issue_refresh(
            user_id=user_id,
            tenant_id=tenant_id,
            family_id=family_id,
            token_version=token_version,
        )

        # 家族只記「目前有效的那一張 jti」。重放偵測就是拿這個值比對——
        # 不需要保留歷史，因為任何不等於它的 jti 都該被當成重放。
        ttl = int((refresh.expires_at - datetime.now(UTC)).total_seconds())
        get_redis().set(
            tenant_key(tenant_id, "refresh-family", str(family_id)), str(refresh.jti), ex=ttl
        )

        return TokenPair(
            access_token=access.token,
            refresh_token=refresh.token,
            expires_at=access.expires_at,
        )

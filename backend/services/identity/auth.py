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
from core import audit
from core.exceptions import (
    AccountLockedError,
    InvalidCredentialsError,
    TokenRevokedError,
)
from core.redis import get_redis, get_script, tenant_key
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import RoleRepository, TenantDirectoryRepository, UserRepository
from services.identity.tokens import (
    ACCESS_TTL,
    AccessClaims,
    IssuedToken,
    TokenCodec,
    get_token_codec,
)
from services.platform.audit import OUTCOME_FAILED, OUTCOME_SUCCEEDED, AuditEvent, AuditService

# 密碼比對的假雜湊：帳號不存在時照樣跑一次驗證，讓兩種失敗的**耗時**也一致。
# 只比對回應內容是不夠的——不存在的帳號若快 100 毫秒回來，時間差本身就是
# 「這個 email 在不在」的答案（10 §2.1 要求 constant-time 行為）。
_DUMMY_HASH = hash_password("timing-equalizer-not-a-real-password")

_ROTATE_ROTATED = "rotated"
_ROTATE_GRACE = "grace"
_ROTATE_REPLAY = "replay"
_ROTATE_REVOKED = "revoked"

# refresh 輪換的原子步驟（見 `AuthService.refresh`）。**比對與改寫必須在同一步**，
# 否則兩個同時到達的請求都會通過比對，然後其中一個發出去的 token 立刻變成「重放」。
#
# KEYS[1] 家族（值 = 目前有效的 jti）、KEYS[2] 這次出示的 jti 的寬限記錄。
# ARGV: 1 出示的 jti、2 新的 jti、3 家族 TTL、4 新的 refresh token、5 寬限秒數。
#
# **寬限記錄存的是 token 本身**，因為輸掉競賽的那個請求必須拿到**贏家那一張**——
# 各自發一張的話，家族只記得住一張，另一張下次使用就是重放（也就是原本的 bug 換個
# 位置發生）。代價是那串 token 在 Redis 裡多活幾秒；把秒數壓到個位數是刻意的，而
# 設成 0 就完全不存。這不是新開一類風險：能讀寫 Redis 的人本來就能改寫家族值，
# 讓一張被竊的 refresh 永遠有效。
_ROTATE_LUA = f"""
local current = redis.call('GET', KEYS[1])
if not current then
    return {{'{_ROTATE_REVOKED}'}}
end
if current == ARGV[1] then
    redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
    if tonumber(ARGV[5]) > 0 then
        redis.call('SET', KEYS[2], ARGV[4], 'EX', ARGV[5])
    end
    return {{'{_ROTATE_ROTATED}'}}
end
local handed_out = redis.call('GET', KEYS[2])
if handed_out then
    return {{'{_ROTATE_GRACE}', handed_out}}
end
redis.call('DEL', KEYS[1])
return {{'{_ROTATE_REPLAY}'}}
"""


def _seconds_until(moment: datetime) -> int:
    """給 Redis 的 TTL。至少 1 秒：``EX 0`` 是語法錯誤，而負數會立刻刪掉那個 key。"""
    return max(int((moment - datetime.now(UTC)).total_seconds()), 1)


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
        audit_log: AuditService | None = None,
    ) -> None:
        self._users = users or UserRepository()
        self._roles = roles or RoleRepository()
        self._directory = directory or TenantDirectoryRepository()
        self._codec = codec or get_token_codec()
        self._settings = get_app_settings()
        self._audit = audit_log or AuditService()

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
        self.ensure_not_locked(tenant_id=tenant_id, email=email)

        # 稽核的 actor：帳號存在時填它的 id，不存在就留白。**在交易外持有**，
        # 因為失敗那一列必須在交易 rollback 之後才寫（見下方 except）。
        attempted_by: uuid.UUID | None = None

        try:
            with tenant_context(tenant_id), unit_of_work():
                user = self._users.get_by_email(email)
                # 帳號不存在時仍然跑一次雜湊驗證（見 _DUMMY_HASH）。
                stored_hash = user.password_hash if user is not None else _DUMMY_HASH
                password_ok = verify_password(password, stored_hash)
                if user is not None:
                    attempted_by = uuid.UUID(str(user.id))

                if user is None or not password_ok or user.status != "active":
                    # 停用的帳號與錯誤的密碼回同一件事：告訴對方「這個帳號被停用了」
                    # 等於確認帳號存在，那是帳號列舉的另一個入口。
                    self.record_password_failure(tenant_id=tenant_id, email=email)
                    raise InvalidCredentialsError

                if needs_rehash(user.password_hash):
                    # 參數調強之後的自動升級——只有此刻手上有明文密碼。
                    self._users.upgrade_password_hash(user.id, hash_password(password))

                roles = self._roles.names_for_user(user.id)
                self._users.touch_last_login(user.id)
                token_version = user.token_version
        except InvalidCredentialsError:
            # **密碼噴發的偵測完全靠這一列**（04 §8.3 明列登入）。寫在交易外：
            # 交易一 rollback，寫在裡面的稽核會跟著消失，而失敗正是最需要留痕的
            # 那一種。刻意不存嘗試的 email／密碼——稽核會被匯出、截圖、進工單。
            self._audit_login(tenant_id, actor_id=attempted_by, outcome=OUTCOME_FAILED)
            raise

        self.clear_password_failures(tenant_id=tenant_id, email=email)
        self._audit_login(tenant_id, actor_id=attempted_by, outcome=OUTCOME_SUCCEEDED)
        return self._issue_pair(
            tenant_id=tenant_id,
            user_id=user.id,
            roles=roles,
            token_version=token_version,
            family_id=uuid.uuid4(),
        )

    def _audit_login(
        self, tenant_id: uuid.UUID, *, actor_id: uuid.UUID | None, outcome: str
    ) -> None:
        """登入的稽核由 service 自己記（2A-4）。

        **請求層記不了**：這條路徑上沒有 principal（還沒有人通過認證），也沒有
        租戶 contextvar（本方法自己進出 `tenant_context`），middleware 拿不到
        tenant_id 就寫不出列。來源欄位（ip／UA／request_id）從稽核 scope 取；
        沒有 scope 就是非請求路徑（CLI、測試），欄位留白。

        帳號鎖定（`AccountLockedError`）不另外記：走到那裡之前，造成鎖定的每一次
        失敗都已經各有一列。
        """
        origin = audit.current_scope()
        self._audit.record(
            tenant_id,
            AuditEvent(
                action="auth.login",
                resource_type="session",
                outcome=outcome,
                request_id=origin.request_id if origin else "",
                actor_id=actor_id,
                # 失敗且查無此人時 actor_id 是空的，但發動者仍然是「某個使用者」
                # ——標成 system 會讓它混進維運 job 的紀錄裡。
                actor_type="user",
                ip=origin.ip if origin else None,
                user_agent=origin.user_agent if origin else "",
            ),
        )

    # ── 換發（rotation + 竊取偵測）──────────────────────────────

    def refresh(self, refresh_token: str) -> TokenPair:
        """換發。**比對與改寫是一步原子操作**，而且剛換掉的那張有數秒寬限期。

        原本是 `GET` 家族 → 比對 jti → （交易之後）`SET` 新 jti 三步，之間沒有任何
        原子性。同一張 refresh 被兩個請求同時使用是**高頻的正常情境**（多分頁同時
        喚醒、前端重試），而它的後果有兩個方向：

        - 兩邊都通過比對、都拿到新 token，家族值被後寫的那個蓋掉——先寫那邊發出去的
          refresh 下次使用時會被判成重放，**整個家族撤銷、正常使用者被隨機登出**。
        - 反過來，真正的攻擊者若恰好與本人同時換發，兩邊都成功，偵測形同失效。

        所以比對與改寫下沉成一段 Lua（`_ROTATE_SCRIPT`）。但原子性只解決一半：兩個
        分頁仍然是兩個請求，而**只有一個能拿到「目前有效的那張 jti」**。因此輸的那個
        走寬限期——拿回**贏家拿到的同一張** refresh token（見 `_ROTATE_SCRIPT` 的
        docstring），於是兩個分頁最後握著同一張，不會有人被踢掉。

        寬限期外仍然是嚴格的：`refresh_rotation_grace_seconds` 秒之後，同一個 jti
        再出現就是重放，家族照樣一起撤銷。設成 0 等於完全關掉這個窗口。
        """
        claims = self._codec.decode_refresh(refresh_token)
        redis = get_redis()
        family_key = tenant_key(claims.tenant_id, "refresh-family", str(claims.family_id))

        # 便宜的先擋：家族不存在就不必查 DB、也不必簽兩張 token。**不做 jti 比對**
        # ——那要與改寫同時發生才有意義，留給下面的 Lua。
        if redis.get(family_key) is None:
            # 家族不存在 = 已被撤銷（或早已過期）。
            raise TokenRevokedError("這個 session 已失效，請重新登入")

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

        access, refresh = self._mint(
            tenant_id=claims.tenant_id,
            user_id=claims.sub,
            roles=roles,
            token_version=token_version,
            family_id=claims.family_id,
        )
        rotated = self._rotate_family(
            tenant_id=claims.tenant_id,
            family_id=claims.family_id,
            presented_jti=claims.jti,
            issued=refresh,
        )
        # 寬限期命中時 `rotated` 是贏家那張；上面剛簽的這張沒有登記進家族，
        # 就這樣丟掉（它不在任何人手上，永遠不會被送回來）。
        return TokenPair(
            access_token=access.token,
            refresh_token=rotated,
            expires_at=access.expires_at,
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

    # ── 密碼嘗試的節流（登入與改密碼共用）────────────────────────
    #
    # **共用同一個計數器是重點，不是省事。** 驗證舊密碼的地方不只登入：
    # `UserService.change_password` 也拿明文密碼去比對，而它只需要一張 access
    # token。分開計數的話，偷到 token 的人可以在那 15 分鐘裡全速猜舊密碼——猜中就
    # 是接管帳號（改掉密碼會順便撤銷原主的所有 session）。同一把鎖之下，那條路徑
    # 的第 5 次失敗就會把整個帳號鎖住，登入那側也一起。

    def ensure_not_locked(self, *, tenant_id: uuid.UUID, email: str) -> None:
        """已達失敗上限就 raise，附上還要等幾秒。"""
        key = self._attempts_key(tenant_id, email)
        redis = get_redis()
        # redis-py 6 的同步 client 與 async client 共用型別，每個命令的回傳都是
        # ``Awaitable | Any``。這裡的 client 一定是同步的（core.redis 的單例），
        # 因此以 cast 收斂——擴散到呼叫端會讓每個運算都要先判型別。
        raw = cast("str | None", redis.get(key))
        if raw is not None and int(raw) >= self._settings.login_max_attempts:
            locked_for = cast("int", redis.ttl(key))
            raise AccountLockedError(retry_after_seconds=max(int(locked_for), 0))

    def record_password_failure(self, *, tenant_id: uuid.UUID, email: str) -> None:
        """失敗計數 +1，並讓整個計數在鎖定時間之後自然消失。

        每次失敗都重設 TTL 是刻意的：持續攻擊會讓鎖定持續延長，而正常使用者
        打錯一兩次之後 15 分鐘就自動恢復，不需要人工解鎖。
        """
        pipeline = get_redis().pipeline()
        key = self._attempts_key(tenant_id, email)
        pipeline.incr(key)
        pipeline.expire(key, self._settings.login_lockout_seconds)
        pipeline.execute()

    def clear_password_failures(self, *, tenant_id: uuid.UUID, email: str) -> None:
        """驗證成功——把計數歸零，免得偶爾打錯幾次的人累積到被鎖。"""
        get_redis().delete(self._attempts_key(tenant_id, email))

    def _attempts_key(self, tenant_id: uuid.UUID, email: str) -> str:
        return tenant_key(tenant_id, "login-fail", email)

    # ── 內部 ────────────────────────────────────────────────────

    def _mint(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        roles: tuple[str, ...],
        token_version: int,
        family_id: uuid.UUID,
    ) -> tuple[IssuedToken, IssuedToken]:
        """簽出一組 token。**不碰 Redis**——登記家族是呼叫端的事，因為登入（開新家族）
        與換發（原子輪換）是兩種完全不同的寫法。"""
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
        return access, refresh

    def _issue_pair(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        roles: tuple[str, ...],
        token_version: int,
        family_id: uuid.UUID,
    ) -> TokenPair:
        """登入用：簽一組 token 並**開一個新家族**。"""
        access, refresh = self._mint(
            tenant_id=tenant_id,
            user_id=user_id,
            roles=roles,
            token_version=token_version,
            family_id=family_id,
        )

        # 家族只記「目前有效的那一張 jti」。重放偵測就是拿這個值比對——
        # 不需要保留歷史，因為任何不等於它的 jti 都該被當成重放（寬限期是唯一的
        # 例外，而那一份存在另一個 key 上，見 `_rotate_family`）。
        get_redis().set(
            tenant_key(tenant_id, "refresh-family", str(family_id)),
            str(refresh.jti),
            ex=_seconds_until(refresh.expires_at),
        )

        return TokenPair(
            access_token=access.token,
            refresh_token=refresh.token,
            expires_at=access.expires_at,
        )

    def _rotate_family(
        self,
        *,
        tenant_id: uuid.UUID,
        family_id: uuid.UUID,
        presented_jti: uuid.UUID,
        issued: IssuedToken,
    ) -> str:
        """原子輪換，回傳**呼叫端該交出去的那張 refresh token**。

        三種結果：換成功（回剛簽的那張）、命中寬限期（回上一次換發時發出去的那張）、
        重放（家族已在 script 裡刪掉，這裡 raise）。
        """
        grace = max(int(self._settings.refresh_rotation_grace_seconds), 0)
        script = get_script(_ROTATE_LUA)
        outcome = cast(
            list[str],
            script(
                keys=[
                    tenant_key(tenant_id, "refresh-family", str(family_id)),
                    tenant_key(tenant_id, "refresh-rotated", str(presented_jti)),
                ],
                args=[
                    str(presented_jti),
                    str(issued.jti),
                    str(_seconds_until(issued.expires_at)),
                    issued.token,
                    str(grace),
                ],
            ),
        )

        if outcome[0] == _ROTATE_ROTATED:
            return issued.token
        if outcome[0] == _ROTATE_GRACE:
            return outcome[1]
        if outcome[0] == _ROTATE_REVOKED:
            # 先擋那一關過了、輪換這一刻卻不見了——同時發生的登出或撤銷。
            raise TokenRevokedError("這個 session 已失效，請重新登入")
        # **重放**：這張 refresh 已經被換過了（且超出寬限期），卻又出現一次 →
        # 有兩個持有者。分不出誰是本人，所以整條 session 鏈一起中止。
        raise TokenRevokedError("偵測到 refresh token 重複使用，已終止此 session")

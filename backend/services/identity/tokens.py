"""JWT 的簽發與驗證（10 §2.1）。

**ES256（非對稱）而非 HS256（共享密鑰）**：HS256 用同一把密鑰簽與驗，於是每個
需要驗 token 的節點都握有偽造能力。ES256 分成私鑰簽、公鑰驗，簽發權集中一處。

**驗證時寫死 ``algorithms=["ES256"]``，絕不看 token 自稱的 alg。** 這一行是整個
檔案最重要的地方：JWT 最有名的漏洞就是「alg 混淆」——攻擊者把 header 改成
``HS256``，拿**公鑰內容當 HMAC 密鑰**簽一張自己編的 token。公鑰是公開的，所以他
拿得到；驗證端若照 token 說的演算法去驗，就會驗出成功。

**``kid`` 從一開始就帶**：它標示這張 token 是哪把金鑰簽的。輪替金鑰時新舊會並存
一段時間（舊 token 還沒過期），沒有 kid 就得每把都試。而且事後才加會讓所有已發出
的 token 失效，所以格式要一次到位——輪替流程本身（JWKS 端點、90 天週期）留到上線
前，這裡只把形狀做對。

**兩種 token 用 ``typ`` claim 區分且不可互換**：access 活 15 分鐘、refresh 活 14 天
且存在 cookie 裡。refresh 被當成 access 接受的話，等於 API 憑證壽命變成兩星期。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import jwt

from config.settings.app_settings import get_app_settings
from core.exceptions import TokenExpiredError, TokenInvalidError

ALGORITHM = "ES256"

TYP_ACCESS = "access"
TYP_REFRESH = "refresh"

# 10 §2.1 的壽命。access 短是因為它沒有即時撤銷以外的煞車；refresh 長是為了讓
# 使用者兩週內不必重新輸密碼，代價由 rotation + 竊取偵測補上。
ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=14)


@dataclass(frozen=True)
class IssuedToken:
    token: str
    jti: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True)
class AccessClaims:
    sub: uuid.UUID
    tenant_id: uuid.UUID
    roles: tuple[str, ...]
    jti: uuid.UUID
    token_version: int
    expires_at: datetime


@dataclass(frozen=True)
class RefreshClaims:
    sub: uuid.UUID
    tenant_id: uuid.UUID
    family_id: uuid.UUID
    jti: uuid.UUID
    token_version: int
    expires_at: datetime


class TokenCodec:
    """簽發與驗證。**不碰資料庫、不碰 Redis**——撤銷狀態由 AuthService 判斷。

    分開的理由是這一層要能單獨測：偽造攻擊的測試只需要金鑰，不需要起 DB。
    """

    def __init__(
        self,
        *,
        private_key_pem: str,
        public_keys: Mapping[str, str],
        active_kid: str,
    ) -> None:
        if active_kid not in public_keys:
            raise ValueError(f"active_kid {active_kid!r} 不在 public_keys 內")
        self._private_key_pem = private_key_pem
        self.public_keys = dict(public_keys)
        self._active_kid = active_kid

    # ── 簽發 ────────────────────────────────────────────────────

    def issue_access(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        roles: tuple[str, ...],
        token_version: int,
        ttl: timedelta = ACCESS_TTL,
    ) -> IssuedToken:
        jti = uuid.uuid4()
        expires_at = datetime.now(UTC) + ttl
        payload = {
            "typ": TYP_ACCESS,
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "roles": list(roles),
            "jti": str(jti),
            "token_version": token_version,
            "exp": expires_at,
            "iat": datetime.now(UTC),
        }
        return IssuedToken(token=self._encode(payload), jti=jti, expires_at=expires_at)

    def issue_refresh(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        family_id: uuid.UUID,
        token_version: int,
        ttl: timedelta = REFRESH_TTL,
    ) -> IssuedToken:
        jti = uuid.uuid4()
        expires_at = datetime.now(UTC) + ttl
        payload = {
            "typ": TYP_REFRESH,
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "family_id": str(family_id),
            "jti": str(jti),
            "token_version": token_version,
            "exp": expires_at,
            "iat": datetime.now(UTC),
        }
        return IssuedToken(token=self._encode(payload), jti=jti, expires_at=expires_at)

    # ── 驗證 ────────────────────────────────────────────────────

    def decode(self, token: str) -> AccessClaims:
        payload = self._decode(token, expected_typ=TYP_ACCESS)
        return AccessClaims(
            sub=uuid.UUID(payload["sub"]),
            tenant_id=uuid.UUID(payload["tenant_id"]),
            roles=tuple(payload.get("roles", ())),
            jti=uuid.UUID(payload["jti"]),
            token_version=int(payload["token_version"]),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )

    def decode_refresh(self, token: str) -> RefreshClaims:
        payload = self._decode(token, expected_typ=TYP_REFRESH)
        return RefreshClaims(
            sub=uuid.UUID(payload["sub"]),
            tenant_id=uuid.UUID(payload["tenant_id"]),
            family_id=uuid.UUID(payload["family_id"]),
            jti=uuid.UUID(payload["jti"]),
            token_version=int(payload["token_version"]),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )

    # ── 內部 ────────────────────────────────────────────────────

    def _encode(self, payload: dict[str, Any]) -> str:
        return jwt.encode(
            payload,
            self._private_key_pem,
            algorithm=ALGORITHM,
            headers={"kid": self._active_kid},
        )

    def _decode(self, token: str, *, expected_typ: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenInvalidError("token 格式錯誤") from exc

        # kid 決定用哪把公鑰。認不得的 kid 直接拒絕——不要「試試看每一把」，
        # 那會讓輪替期間的錯誤金鑰靜默地繼續有效。
        public_key = self.public_keys.get(str(header.get("kid")))
        if public_key is None:
            raise TokenInvalidError("未知的簽章金鑰 (kid)")

        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                public_key,
                # 寫死演算法——這一行擋掉 alg 混淆攻擊，見模組 docstring。
                algorithms=[ALGORITHM],
                options={"require": ["exp", "sub", "jti", "typ"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpiredError("token 已過期") from exc
        except jwt.PyJWTError as exc:
            raise TokenInvalidError("token 驗證失敗") from exc

        if payload.get("typ") != expected_typ:
            raise TokenInvalidError(f"token 類型不符（需要 {expected_typ}）")

        return payload


@lru_cache(maxsize=1)
def get_token_codec() -> TokenCodec:
    """從設定載入金鑰，組出全程序共用的 codec。

    金鑰缺檔時**直接爆炸**並指出補救指令（Fail Fast，鐵則 9）。自動產生一組
    暫時金鑰看起來友善，但那會讓「忘了掛金鑰」的部署照樣起得來——每次重啟所有
    人的 token 都失效，而且症狀是「使用者隨機被登出」，很難查到根因。
    """
    settings = get_app_settings()
    try:
        private_pem = settings.jwt_private_key_path.read_text(encoding="utf-8")
        public_pem = settings.jwt_public_key_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:  # pragma: no cover —— 環境問題，不是邏輯分支
        raise RuntimeError(
            f"找不到 JWT 金鑰（{exc.filename}）——本機請跑 `make gen-jwt-keys`；"
            "部署環境由 Secrets Manager 掛載"
        ) from exc

    return TokenCodec(
        private_key_pem=private_pem,
        public_keys={settings.jwt_active_kid: public_pem},
        active_kid=settings.jwt_active_kid,
    )

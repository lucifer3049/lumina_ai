"""驗收：JWT 的簽發與驗證（10 §2.1）。

**為什麼是 ES256（非對稱）而不是 HS256（共享密鑰）**：HS256 用同一把密鑰簽與驗，
於是每個需要驗證 token 的節點都握有偽造 token 的能力。ES256 分成私鑰簽、公鑰驗，
簽發權集中在一處。

本檔驗兩件事：claims 的內容正確，以及**四種偽造手法都被擋下**。後者是重點——
JWT 的實作陷阱幾乎全在驗證那一側，而錯誤的驗證程式對正常 token 的行為完全正常，
只有攻擊者會發現差別：

1. 改內容不改簽章 → 必須被簽章驗證擋下。
2. 換一把自己的金鑰去簽 → 必須因為公鑰對不上而被擋下。
3. **alg 混淆**：把 header 的 ``alg`` 改成 ``HS256``，然後拿**公鑰當共享密鑰**去簽。
   公鑰是公開的，所以攻擊者拿得到。驗證端若照 token 自稱的 alg 去驗，就會用公鑰
   當 HMAC 密鑰驗成功——這是 JWT 最著名的漏洞，而且只有明確鎖定演算法才擋得住。
4. **token 類型混用**：拿 refresh token 當 access token 用。refresh 壽命 14 天且
   目的只有換發，被當成 access 接受等於憑證壽命暴增。

金鑰在測試裡即時產生（不讀 .env）：測試不該依賴本機有沒有跑過 `make gen-jwt-keys`，
而且第 2、3 條本來就需要「另一把金鑰」。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from core.exceptions import TokenInvalidError
from services.identity.tokens import TokenCodec

TENANT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
USER_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
KID = "test-key-1"


def _keypair() -> tuple[str, str]:
    """產一組 P-256 金鑰，回傳 (私鑰 PEM, 公鑰 PEM)。"""
    key = ec.generate_private_key(ec.SECP256R1())
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture
def codec() -> TokenCodec:
    private_pem, public_pem = _keypair()
    return TokenCodec(private_key_pem=private_pem, public_keys={KID: public_pem}, active_kid=KID)


@pytest.fixture
def other_codec() -> TokenCodec:
    """另一組金鑰——用來模擬「攻擊者自己簽一張」。"""
    private_pem, public_pem = _keypair()
    return TokenCodec(private_key_pem=private_pem, public_keys={KID: public_pem}, active_kid=KID)


def _issue_access(codec: TokenCodec, **overrides: object) -> str:
    kwargs: dict[str, object] = {
        "user_id": USER_ID,
        "tenant_id": TENANT_ID,
        "roles": ("owner",),
        "token_version": 1,
    }
    kwargs.update(overrides)
    return codec.issue_access(**kwargs).token  # type: ignore[arg-type]


# ── claims 內容 ────────────────────────────────────────────────


def test_access_token_carries_the_documented_claims(codec: TokenCodec) -> None:
    """10 §2.1 指名的五個 claim 一個都不能少。

    `token_version` 特別重要：改密碼或停用帳號時把它 +1，就能讓該使用者手上所有
    尚未過期的 token 一次失效。少了它，唯一的撤銷手段是逐一把 jti 加進撤銷名單，
    而登出以外的情境（帳號被盜）根本不知道有哪些 jti。
    """
    claims = codec.decode(_issue_access(codec))

    assert claims.sub == USER_ID
    assert claims.tenant_id == TENANT_ID
    assert claims.roles == ("owner",)
    assert claims.token_version == 1
    assert isinstance(claims.jti, uuid.UUID)


def test_token_header_declares_es256_and_a_kid(codec: TokenCodec) -> None:
    """header 要有 ``kid``，即使現在只有一把金鑰。

    ``kid`` 是「這張 token 是用哪把金鑰簽的」。輪替金鑰時新舊金鑰會並存一段時間
    （舊 token 還沒過期），沒有 kid 就只能每把都試一次；而且**事後加 kid 會讓所有
    既發出的 token 失效**，所以形狀要一開始就對。金鑰輪替的流程本身留到上線前做。
    """
    header = jwt.get_unverified_header(_issue_access(codec))

    assert header["alg"] == "ES256"
    assert header["kid"] == KID


# ── 四種偽造手法 ────────────────────────────────────────────────


def test_expired_token_is_rejected(codec: TokenCodec) -> None:
    from core.exceptions import TokenExpiredError

    expired = codec.issue_access(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        roles=("owner",),
        token_version=1,
        ttl=timedelta(seconds=-1),
    ).token

    with pytest.raises(TokenExpiredError):
        codec.decode(expired)


def test_tampered_payload_is_rejected(codec: TokenCodec) -> None:
    """改了內容但沿用原簽章——最直覺的偽造，靠簽章驗證擋下。"""
    header, payload, signature = _issue_access(codec).split(".")
    forged_payload = jwt.utils.base64url_encode(
        jwt.utils.base64url_decode(payload).replace(b'"owner"', b'"admin"')
    ).decode()

    with pytest.raises(TokenInvalidError):
        codec.decode(f"{header}.{forged_payload}.{signature}")


def test_token_signed_with_another_key_is_rejected(
    codec: TokenCodec, other_codec: TokenCodec
) -> None:
    """攻擊者自備金鑰簽一張、連 kid 都抄對——公鑰對不上就是對不上。"""
    with pytest.raises(TokenInvalidError):
        codec.decode(_issue_access(other_codec))


def test_algorithm_confusion_with_the_public_key_is_rejected(codec: TokenCodec) -> None:
    """把 alg 換成 HS256、用**公鑰內容**當 HMAC 密鑰簽——經典的 alg 混淆攻擊。

    公鑰是公開資訊，攻擊者本來就拿得到。驗證端若「照 token 自稱的 alg 去驗」，
    就會拿公鑰當共享密鑰驗出成功，於是任何人都能簽任意 claims。唯一的防法是
    驗證時**明確指定只接受 ES256**，不看 token 怎麼說。
    """
    public_pem = codec.public_keys[KID]

    # 手工組這張 token，不用 PyJWT 的 encode()：PyJWT 在**簽發**端就擋掉「拿 PEM
    # 當 HMAC 密鑰」（InvalidKeyError）。但攻擊者不會用我們的函式庫——他只要三段
    # base64 加一個 HMAC 就好。要驗的是**我們的驗證端**擋不擋得住，所以偽造必須
    # 繞過 PyJWT 的貼心防護，否則這條測試驗到的是 PyJWT 的簽發端，不是我們。
    header = jwt.utils.base64url_encode(
        json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode()
    )
    payload = jwt.utils.base64url_encode(
        json.dumps(
            {
                "sub": str(USER_ID),
                "tenant_id": str(TENANT_ID),
                "roles": ["owner"],
                "jti": str(uuid.uuid4()),
                "token_version": 1,
                "typ": "access",
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            }
        ).encode()
    )
    signing_input = header + b"." + payload
    signature = jwt.utils.base64url_encode(
        hmac.new(public_pem.encode(), signing_input, hashlib.sha256).digest()
    )
    forged = (signing_input + b"." + signature).decode()

    with pytest.raises(TokenInvalidError):
        codec.decode(forged)


def test_refresh_token_is_not_accepted_as_an_access_token(codec: TokenCodec) -> None:
    """兩種 token 用途不同、壽命差 1300 倍，不可互換。

    refresh 活 14 天且只該用在 ``/auth/refresh``；被當成 access 接受的話，等於
    使用者的 API 憑證從 15 分鐘變成兩星期，而且它還存在 cookie 裡。
    """
    refresh = codec.issue_refresh(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        family_id=uuid.uuid4(),
        token_version=1,
    ).token

    with pytest.raises(TokenInvalidError):
        codec.decode(refresh)


def test_access_token_is_not_accepted_as_a_refresh_token(codec: TokenCodec) -> None:
    """反向也要擋：拿 access token 去換新的一組，等於繞過 rotation 的竊取偵測。"""
    with pytest.raises(TokenInvalidError):
        codec.decode_refresh(_issue_access(codec))

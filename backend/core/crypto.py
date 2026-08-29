"""欄位級加密的底層（10 §5 的 envelope encryption，2C-2）。

10 §5：「provider API key、同步來源憑證——KEK 於 Secrets Manager、DEK per-tenant、
AES-256-GCM；密文入 DB（`credential_ref` 指向）；解密僅在使用當下、不落 log。」

**這一層只做密碼學，不碰 DB**：KEK 怎麼讀、明文怎麼封裝。per-tenant 的 DEK 與落地在
`services/platform/credentials.py`——分開的理由是這裡要能在沒有資料庫的 unit 層被驗，
而「加密對不對」正是最該被獨立驗的一件事。

放 `core/` 與 `core/redis.py`、`core/object_storage.py` 一致：每一個外部資源在 repo
內只有一個入口，金鑰也是。

## KEK 的來源（2C-0 定案，2026-08-29）

env `ENCRYPTION_KEK`（32 bytes 的 base64）優先，缺了退回 `backend/.secrets/` 的檔案
（本機由 `make gen-kek` 產生），兩者都沒有就 **Fail Fast**——與 JWT 金鑰逐字相同的
慣例（`services/identity/tokens.py::get_token_codec` 的 docstring 講了為什麼不自動
產生一把暫時金鑰：那會讓「忘了掛金鑰」的部署照樣起得來，而症狀出現在很久以後）。

**env 優先於檔案**：部署環境注入的是環境變數（10 §6）。反過來的話，一台留著舊
`.secrets/` 的機器會安靜地用舊金鑰，而症狀是「某些資料解不開」。
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "KEY_BYTES",
    "MissingKekError",
    "generate_key",
    "load_kek",
    "open_sealed",
    "seal",
]

# AES-256。**驗長度是必要的**：16 bytes 是合法的 AES-128 金鑰，`cryptography` 會照
# 跑，於是「我們以為的 256 位加密」其實是 128 位——沒有任何錯誤訊息。
KEY_BYTES = 32
# GCM 的標準 nonce 長度（96 bits）。
_NONCE_BYTES = 12


class MissingKekError(RuntimeError):
    """KEK 缺席或形狀不對。**訊息絕不帶金鑰內容**（鐵則 9）。"""


def generate_key() -> bytes:
    """一把新的 256 位金鑰（`make gen-kek` 與 per-tenant DEK 共用）。"""
    return secrets.token_bytes(KEY_BYTES)


def load_kek(*, env_value: str | None = None, path: Path | None = None) -> bytes:
    """主金鑰：env → 檔案 → Fail Fast。

    參數是給測試用的注入口；正式路徑一個都不傳，走設定與環境變數。
    """
    if env_value is None:
        env_value = os.environ.get("ENCRYPTION_KEK")
    if path is None:
        from config.settings.app_settings import get_app_settings

        path = get_app_settings().encryption_kek_path

    if env_value is not None and env_value.strip():
        return _decoded(env_value, source="ENCRYPTION_KEK")

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MissingKekError(
            "找不到加密主金鑰（KEK）——本機請跑 `make gen-kek`；"
            "部署環境請注入 ENCRYPTION_KEK（32 bytes 的 base64）"
        ) from exc
    return _decoded(raw, source=str(path))


@lru_cache(maxsize=1)
def get_kek() -> bytes:
    """全程序共用的 KEK。

    快取的理由與 `get_token_codec` 相同：每次解密都讀一次檔案是白花的 syscall，而
    金鑰在行程存活期間不會變（輪替走重啟）。
    """
    return load_kek()


def seal(key: bytes, plaintext: str) -> bytes:
    """AES-256-GCM 封裝，回 ``nonce || ciphertext+tag``。

    **nonce 每次隨機且與密文一起存**：GCM 的 nonce 在同一把金鑰下重用會讓明文可被
    還原，而密文看起來完全正常。分兩個欄位存的話，日後任何一次「只複製密文」的維運
    動作都會產生一批永遠解不開的資料。
    """
    _require_key(key)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)


def open_sealed(key: bytes, blob: bytes) -> str:
    """拆封。**密文被改過會 raise**（GCM 的驗證標籤）。

    沒有驗證標籤的模式（CTR/CBC）解得出來的是垃圾，而程式會把那串垃圾當成 API key
    送給 provider——錯誤訊息會指向 provider，而根因在這裡。
    """
    _require_key(key)
    nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")


def _decoded(raw: str, *, source: str) -> bytes:
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        # **不回傳收到的值**：它會進 log 與 500 的錯誤路徑（鐵則 9）。
        raise MissingKekError(f"加密主金鑰（{source}）不是合法的 base64") from exc
    if len(key) != KEY_BYTES:
        raise MissingKekError(
            f"加密主金鑰（{source}）長度不是 {KEY_BYTES} bytes——"
            "AES-256 需要 32 bytes；`make gen-kek` 產的就是這個長度"
        )
    return key


def _require_key(key: bytes) -> None:
    if len(key) != KEY_BYTES:
        raise MissingKekError(f"金鑰長度不是 {KEY_BYTES} bytes")

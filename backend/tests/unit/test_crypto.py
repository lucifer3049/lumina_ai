"""驗收：envelope 加密的底層（10 §5「欄位級加密」，工作包 2C-2）。

10 §5 那一行寫的是「KEK 於 Secrets Manager、DEK per-tenant、AES-256-GCM；密文入 DB
（`credential_ref` 指向）；解密僅在使用當下、不落 log」。**KEK 的來源於 2C-0 定案**：
env `ENCRYPTION_KEK`（32 bytes base64）優先，缺了退回 `backend/.secrets/` 的檔案，
兩者都沒有就 Fail Fast 並指出 `make gen-kek`——與 JWT 金鑰逐字相同的慣例
（`services/identity/tokens.py::get_token_codec`）。

這一檔只驗**不碰 DB 的那一半**：金鑰怎麼讀進來、密文長什麼樣。per-tenant 的 DEK 與
落地在 `tests/integration/test_credentials.py`。

四個錯了都不會有例外的地方：

1. **金鑰長度沒驗**。AES-256 要 32 bytes；給 16 bytes 的話 `cryptography` 會照跑
   （那是合法的 AES-128 金鑰），於是「我們以為的 256 位加密」其實是 128 位。
2. **nonce 重複**。GCM 的 nonce 一旦在同一把金鑰下重用，**明文可被還原**——而密文
   看起來完全正常。所以 nonce 必須每次隨機，且與密文一起存。
3. **沒有驗證標籤**（用 CTR/CBC 而不是 GCM）：密文被改一個 byte 之後解得出來的是
   垃圾，而程式會把那串垃圾當成 API key 送出去。
4. **明文進 log 或 repr**。這一層的物件會被塞進 structlog 事件與例外訊息裡（鐵則 9），
   而那些地方沒有人在看的時候才會出事。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

from core.crypto import (
    KEY_BYTES,
    MissingKekError,
    load_kek,
    open_sealed,
    seal,
)


def _key(fill: bytes = b"\x01") -> bytes:
    return fill * KEY_BYTES


class TestKeyLoading:
    def test_the_env_var_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """部署環境注入的是環境變數（10 §6 的 Secrets Manager → env）。

        檔案優先的話，一台留著舊 `.secrets/` 的機器會安靜地用舊金鑰——而症狀是
        「某些資料解不開」，出現在幾週之後。
        """
        path = tmp_path / "encryption.kek"
        path.write_text(base64.b64encode(_key(b"\x02")).decode(), encoding="utf-8")
        monkeypatch.setenv("ENCRYPTION_KEK", base64.b64encode(_key(b"\x01")).decode())

        assert load_kek(env_value=None, path=path) != _key(b"\x02"), "環境變數沒有被優先採用"

    def test_the_file_is_the_fallback(self, tmp_path: Path) -> None:
        """本機開發由 `make gen-kek` 產生到 `backend/.secrets/`（與 JWT 金鑰同一處）。"""
        path = tmp_path / "encryption.kek"
        path.write_text(base64.b64encode(_key()).decode(), encoding="utf-8")

        assert load_kek(env_value=None, path=path) == _key()

    def test_missing_everywhere_fails_fast_with_the_fix(self, tmp_path: Path) -> None:
        """**自動產生一把暫時金鑰看起來友善，實際上是資料遺失**：每次重啟換一把，
        於是上一輪存的憑證全部解不開，而錯誤訊息會指向 provider 而不是金鑰。
        """
        with pytest.raises(MissingKekError) as missing:
            load_kek(env_value=None, path=tmp_path / "nope.kek")

        assert "make gen-kek" in str(missing.value), "錯誤訊息要說得出怎麼修"

    @pytest.mark.parametrize(
        "bad", ["", "   ", "not-base64!!", base64.b64encode(b"short").decode()]
    )
    def test_a_key_that_is_not_32_bytes_is_rejected(self, bad: str, tmp_path: Path) -> None:
        """16 bytes 是合法的 AES-128 金鑰——不驗長度的話，`cryptography` 會照跑，
        而我們以為的 256 位加密其實是 128 位（本檔第 1 條）。"""
        with pytest.raises(MissingKekError):
            load_kek(env_value=bad, path=tmp_path / "nope.kek")

    def test_the_key_never_appears_in_the_error(self, tmp_path: Path) -> None:
        """訊息會進 log 與 500 的錯誤路徑（鐵則 9：secrets 不進 log、不進錯誤訊息）。"""
        leaked = base64.b64encode(b"x" * 8).decode()

        with pytest.raises(MissingKekError) as rejected:
            load_kek(env_value=leaked, path=tmp_path / "nope.kek")

        assert leaked not in str(rejected.value)


class TestSealing:
    def test_round_trip(self) -> None:
        assert open_sealed(_key(), seal(_key(), "sk-secret")) == "sk-secret"

    def test_two_seals_of_the_same_text_differ(self) -> None:
        """nonce 每次隨機（本檔第 2 條）。相同的話，同一把金鑰下的重用會讓明文可還原
        ——而密文看起來完全正常。**這一條同時擋掉「拿密文當去重鍵」那種用法。**"""
        assert seal(_key(), "same") != seal(_key(), "same")

    def test_a_tampered_ciphertext_is_rejected(self) -> None:
        """GCM 的驗證標籤（本檔第 3 條）：沒有它，改過的密文會解出垃圾，而程式會把
        那串垃圾當成 API key 送給 provider。"""
        blob = bytearray(seal(_key(), "sk-secret"))
        blob[-1] ^= 0x01

        with pytest.raises(Exception):  # noqa: B017 —— 底層例外型別屬 cryptography
            open_sealed(_key(), bytes(blob))

    def test_another_key_cannot_open_it(self) -> None:
        """per-tenant DEK 的隔離最終靠這一條：拿到別人的密文也解不開。"""
        blob = seal(_key(b"\x01"), "sk-secret")

        with pytest.raises(Exception):  # noqa: B017
            open_sealed(_key(b"\x02"), blob)

    def test_the_blob_carries_its_own_nonce(self) -> None:
        """nonce 與密文一起存（不另開欄位）：分兩欄的話，日後任何一次「只複製密文」
        的維運動作都會產生一批永遠解不開的資料。"""
        blob = seal(_key(), "x")

        assert len(blob) > 12, "至少要裝得下 96-bit nonce"
        assert open_sealed(_key(), blob) == "x"

    def test_the_plaintext_is_not_recoverable_by_eye(self) -> None:
        """最低限度的健全性檢查：密文裡不得看得到明文。"""
        blob = seal(_key(), "sk-live-1234567890")

        assert b"sk-live" not in blob
        assert not re.search(rb"sk-live", base64.b64encode(blob))

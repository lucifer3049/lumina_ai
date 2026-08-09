"""驗收：密碼雜湊（10 §2.1）。

**為什麼是 argon2id 而不是 bcrypt/PBKDF2**：argon2id 是 memory-hard 的——破解者
用 GPU 平行嘗試時，瓶頸從「算力」變成「記憶體頻寬」，同樣的硬體能試的次數少上
好幾個數量級。bcrypt 只吃 CPU，現代 GPU 農場對它非常有效率。

本檔全部是純函式測試，不需要資料庫。四類斷言各自對應一種真實事故：

1. **每次雜湊都不同**（有隨機 salt）：相同的雜湊值代表相同密碼，資料庫外洩時
   攻擊者能直接看出哪些使用者共用密碼，也讓彩虹表重新可用。
2. **雜湊字串裡不含明文**：聽起來理所當然，但自製「加鹽 + 明文拼接」的實作
   真的會把明文寫進去。
3. **參數達到下限**：argon2 的強度完全由參數決定，用預設值以外的弱參數（例如
   記憶體開到 8 KiB）跑出來的雜湊看起來一模一樣，只有解析字串才看得出來。
4. **參數變強時舊雜湊要能被識別出需要重算**：否則調高參數只對新使用者生效，
   而那件事沒有任何可見的症狀。
"""

from __future__ import annotations

import re

from common.passwords import hash_password, needs_rehash, verify_password

PASSWORD = "correct horse battery staple"

# OWASP Password Storage Cheat Sheet 的 argon2id 下限：19 MiB 記憶體、2 次迭代、
# 1 個平行度。低於這組值的雜湊在現代硬體上破解成本會低一個數量級以上。
MIN_MEMORY_KIB = 19456
MIN_TIME_COST = 2

_ENCODED = re.compile(r"^\$argon2id\$v=(?P<v>\d+)\$m=(?P<m>\d+),t=(?P<t>\d+),p=(?P<p>\d+)\$")


def test_same_password_hashes_differently_each_time() -> None:
    assert hash_password(PASSWORD) != hash_password(PASSWORD), (
        "兩次雜湊結果相同 → 沒有隨機 salt，外洩時可直接比對出共用密碼的帳號"
    )


def test_verify_accepts_the_original_password() -> None:
    assert verify_password(PASSWORD, hash_password(PASSWORD)) is True


def test_verify_rejects_a_wrong_password() -> None:
    assert verify_password("not the password", hash_password(PASSWORD)) is False


def test_verify_rejects_a_corrupted_hash_instead_of_raising() -> None:
    """雜湊字串壞掉（截斷、被改過）要回 False，不能讓例外冒到呼叫端。

    登入流程對「驗證失敗」與「例外」的處理完全不同：前者回 401，後者是 500。
    一列壞掉的資料不該讓登入端點回 500——那既洩漏了資料狀態，也會讓監控誤判為
    系統故障。
    """
    assert verify_password(PASSWORD, "$argon2id$totally-broken") is False


def test_encoded_hash_never_contains_the_plaintext() -> None:
    assert PASSWORD not in hash_password(PASSWORD)


def test_parameters_meet_the_minimum_strength() -> None:
    """雜湊字串裡的參數要達到下限——弱參數的雜湊外觀完全正常，只能解析才知道。"""
    match = _ENCODED.match(hash_password(PASSWORD))

    assert match is not None, "雜湊不是 argon2id 編碼格式"
    assert int(match["m"]) >= MIN_MEMORY_KIB, f"記憶體成本 {match['m']} KiB 低於下限"
    assert int(match["t"]) >= MIN_TIME_COST, f"迭代次數 {match['t']} 低於下限"


def test_hash_from_weaker_parameters_is_flagged_for_rehash() -> None:
    """參數調強之後，舊雜湊必須被判定為「需要重算」。

    重算只能在使用者下次登入、手上有明文時做。少了這個判斷，調參只對新帳號
    生效，既有帳號永遠停在舊強度——而且完全看不出來。
    """
    weak = f"$argon2id$v=19$m={MIN_MEMORY_KIB // 4},t=1,p=1$c2FsdHNhbHQ$aGFzaGhhc2g"

    assert needs_rehash(weak) is True
    assert needs_rehash(hash_password(PASSWORD)) is False

"""密碼雜湊 —— argon2id（10 §2.1）。

**為什麼是 argon2id**：它是 memory-hard 的。破解者用 GPU 平行嘗試時，瓶頸從
「算力」變成「記憶體頻寬」，同樣的硬體能試的次數少上好幾個數量級。bcrypt 只吃
CPU，對現代 GPU 農場來說便宜得多。

參數取 OWASP Password Storage Cheat Sheet 的 argon2id 下限（19 MiB / t=2 / p=1）
再往上一級。這是**登入延遲與破解成本的取捨**：參數愈高，每次登入愈慢（使用者
等待），破解也愈貴。調整時記得 :func:`needs_rehash` 會讓既有雜湊在下次登入時
自動升級——沒有那個機制的話，調參只對新帳號生效，而且完全看不出來。

本模組刻意不知道 User、不知道租戶：它是純函式，所以放在 `common/`。
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# OWASP 下限是 m=19456(19MiB), t=2, p=1。這裡 m 取 64 MiB：本專案的登入不是熱路徑
# （每個 session 一次），多花的幾十毫秒換取數倍的破解成本很划算。
_HASHER = PasswordHasher(memory_cost=65536, time_cost=3, parallelism=2)


def hash_password(raw_password: str) -> str:
    """回傳 argon2id 編碼字串（含 salt 與參數，無需另外存欄位）。"""
    return _HASHER.hash(raw_password)


def verify_password(raw_password: str, encoded_hash: str) -> bool:
    """驗證密碼；**任何失敗都回 False，不丟例外**。

    壞掉的雜湊字串（截斷、被改過、格式不同）如果讓例外冒到呼叫端，登入端點會
    回 500——那既洩漏了資料狀態，也會讓監控把一列壞資料誤判成系統故障。驗證
    失敗與資料壞掉對使用者而言是同一件事：這組憑證不能用。
    """
    try:
        return _HASHER.verify(encoded_hash, raw_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(encoded_hash: str) -> bool:
    """這個雜湊是不是用比現行設定更弱的參數算的。

    重算只能在使用者下次登入、手上有明文時做。少了這個判斷，調高參數只對新帳號
    生效，既有帳號永遠停在舊強度，而且沒有任何症狀。
    """
    try:
        return _HASHER.check_needs_rehash(encoded_hash)
    except InvalidHashError:
        # 壞掉的雜湊當然「需要重算」，但呼叫端只在驗證成功後才問這件事，
        # 所以這裡走不到；回 True 是保守值。
        return True

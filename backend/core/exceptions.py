"""業務例外階層 —— 全 repo 唯一（04 附錄 B.1）。

規則（04 §B.1）：
- HTTP 轉換**只發生在** ``api/exception_handlers.py`` 單點。
- ``services/`` 以下只 raise 本檔例外，永不 raise ``HTTPException``。
- ``worker/`` 捕捉後轉 job status + 通知，不吞例外。

設計原則（04 §B.2）：「失敗是流程一部分」的方法（tool、串流、檢索降級）以
**回傳值**表達失敗；「失敗即不可續行」的方法才 raise。

SPIKE 範圍說明：本檔目前只實作 ADR-001 穿刺驗證用得到的節點。完整階層
（ProviderError / ToolError / EtlError / Quota…）與 09 附錄 A 的 27 個
錯誤 code 於 Phase 0 全量時補齊——刻意不先寫半套 enum，避免與契約字典漂移。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """對映 09 附錄 A 錯誤 code 字典。新增 code 視同 API 契約變更（需 review）。"""

    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"  # 404：不存在或無權可見（合併，防枚舉）
    VALIDATION_FAILED = "VALIDATION_FAILED"  # 422：語意驗證失敗（errors[] 帶欄位明細）
    INTERNAL_ERROR = "INTERNAL_ERROR"  # 500：未預期錯誤（不洩細節，附 request_id）
    # 認證（1A-3）。四個都對映 09 附錄 A；client 只該依 code 分支，不該解析 detail。
    AUTH_REQUIRED = "AUTH_REQUIRED"  # 401：沒帶憑證
    AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"  # 401：帳密錯誤（不區分帳號是否存在）
    # 下兩行的 noqa: S105 —— 錯誤碼常數，不是密碼；bandit 只看到名字裡有 TOKEN。
    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"  # noqa: S105 —— 401：過期 → client 走 refresh
    AUTH_TOKEN_REVOKED = "AUTH_TOKEN_REVOKED"  # noqa: S105 —— 401：已撤銷（denylist / token_version）
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"  # 423：連續失敗達上限，暫時鎖定
    # 授權與資料衝突（1A-4）
    PERMISSION_DENIED = "PERMISSION_DENIED"  # 403：功能類權限不足（資源類回 404）
    RESOURCE_CONFLICT = "RESOURCE_CONFLICT"  # 409：唯一性或狀態機衝突


class DomainError(Exception):
    """業務例外基底。``code`` 對映 09 附錄 A。"""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = details or {}

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class NotFoundError(DomainError):
    """→ 404。``Repository.get()`` 找不到即 raise（get 語意 = 必存在）。"""

    code = ErrorCode.RESOURCE_NOT_FOUND


class TenantContextMissingError(DomainError):
    """→ 500 + P1 告警。

    這**不是**使用者錯誤，是程式錯誤：某段程式碼在沒有租戶上下文的情況下
    碰了 tenant-scoped 資源。ADR-002 要求 Fail Fast——不提供「預設租戶」或
    「無租戶模式」的退路，那是跨租戶洩漏的溫床。
    """

    code = ErrorCode.INTERNAL_ERROR

    def __init__(self, operation: str | None = None) -> None:
        detail = f"（操作：{operation}）" if operation else ""
        super().__init__(
            f"TenantContext 缺失，拒絕存取 tenant-scoped 資源{detail}",
            details={"operation": operation} if operation else None,
        )


class AuthenticationError(DomainError):
    """認證失敗的共同基底 → 全部 401。

    子類別分開存在是為了讓 **client 能分辨下一步該做什麼**：過期要去 refresh、
    撤銷要重新登入、帳密錯誤要請使用者重打。但「帳號不存在」與「密碼錯誤」
    **刻意共用同一個**（:class:`InvalidCredentialsError`）——分開會讓這個端點
    變成帳號列舉工具（10 §2.1）。
    """

    code = ErrorCode.AUTH_REQUIRED


class InvalidCredentialsError(AuthenticationError):
    """→ 401。帳號不存在、密碼錯誤、租戶 slug 不存在，三者回應完全相同。"""

    code = ErrorCode.AUTH_INVALID_CREDENTIALS

    def __init__(self) -> None:
        # 訊息固定字串：任何隨情境變化的細節都會變成側信道。
        super().__init__("帳號或密碼錯誤")


class TokenInvalidError(AuthenticationError):
    """→ 401。簽章不符、演算法不符、類型不符、格式壞掉。"""

    code = ErrorCode.AUTH_REQUIRED


class TokenExpiredError(AuthenticationError):
    """→ 401。client 收到這個 code 應該去打 ``/auth/refresh`` 而不是要使用者重登。"""

    code = ErrorCode.AUTH_TOKEN_EXPIRED


class TokenRevokedError(AuthenticationError):
    """→ 401。登出、refresh 家族被判定竊取、或 ``token_version`` 被拉高。"""

    code = ErrorCode.AUTH_TOKEN_REVOKED


class AccountLockedError(AuthenticationError):
    """→ 423。連續登入失敗達上限。

    鎖定期間**連正確密碼也不放行**：只擋錯誤密碼的話，暴力破解只是變慢，猜中
    的那一次照樣通過（10 §2.1）。
    """

    code = ErrorCode.ACCOUNT_LOCKED

    def __init__(self, *, retry_after_seconds: int) -> None:
        super().__init__(
            "帳號已暫時鎖定，請稍後再試",
            details={"retry_after_seconds": retry_after_seconds},
        )


class PermissionDeniedError(DomainError):
    """→ 403。**功能類**權限不足（10 §3）。

    與資源類的分野很重要：「你不能建立使用者」回 403，因為這個功能的存在本身
    不是秘密，告訴你要去要權限是合理的。而「那份文件屬於別的租戶」回 404
    （:class:`NotFoundError`）——回 403 等於承認那個 id 存在，讓人可以拿 id 掃出
    別的租戶有哪些資源。
    """

    code = ErrorCode.PERMISSION_DENIED

    def __init__(self, *, required: str) -> None:
        super().__init__("權限不足", details={"required_permission": required})


class ConflictError(DomainError):
    """→ 409。唯一性或狀態機衝突（例如同租戶內 email 重複）。

    這類情況是**使用者可以自己修正**的（換一個信箱），所以不能讓 DB 的唯一約束
    直接冒成 500——那會把可預期的衝突記成系統故障，淹掉真正需要人看的告警。
    """

    code = ErrorCode.RESOURCE_CONFLICT


class CrossTenantTransactionError(DomainError):
    """→ 500 + P1 告警。

    同一個交易內出現兩個租戶。與 :class:`TenantContextMissingError` 同樣是程式
    錯誤而非使用者錯誤：``app.tenant_id`` 是交易區域參數，一個交易只能有一個值，
    因此「一個交易服務兩個租戶」在 RLS 之下沒有正確語意可言。詳見 core/uow.py。
    """

    code = ErrorCode.INTERNAL_ERROR

    def __init__(self, *, active: str, requested: str) -> None:
        super().__init__(
            f"交易已綁定租戶 {active}，不得在其中改用租戶 {requested}——跨租戶操作請各自開交易",
            details={"active_tenant": active, "requested_tenant": requested},
        )

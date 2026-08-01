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

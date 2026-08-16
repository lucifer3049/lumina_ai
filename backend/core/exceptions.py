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
    # 上傳（1B-3）
    UPLOAD_TOO_LARGE = "UPLOAD_TOO_LARGE"  # 413：超過單請求大小上限
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"  # 415：MIME 白名單外（magic bytes 判定）
    # AI provider（1C-1）。四個都對映 09 附錄 A。
    RATE_LIMITED = "RATE_LIMITED"  # 429：頻率限制（可重試，帶 Retry-After）
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"  # 429：配額用盡（重試無用；額度屬 2A）
    MODEL_NOT_ENABLED = "MODEL_NOT_ENABLED"  # 422：模型未對本租戶啟用
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"  # 503：provider 不可用／fallback 鏈耗盡
    # ETL（1B-4）。09 附錄 A 標明它**不對映 HTTP**：抽取跑在 worker 裡，失敗會落到
    # `etl_jobs.error` 與通知，沒有一個等在旁邊的請求可以回。因此 api/main.py 的
    # `_HTTP_STATUS` 刻意不收這一條——真的漏到 HTTP 時走 500，那是程式錯誤。
    ETL_FAILED = "ETL_FAILED"
    # 串流對話（1D-3a）。同樣**不對映 HTTP**（09 附錄 A 的 HTTP 欄是「—」），理由與
    # ETL_FAILED 相同但情境相反：不是沒有請求在等，而是那個請求**早就回了 200**——
    # 中斷發生在第一個 token 之後，狀態碼已經送出去了。它只能是 SSE 的 error event。
    STREAM_INTERRUPTED = "STREAM_INTERRUPTED"


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


class ValidationFailedError(DomainError):
    """→ 422。語意層面的輸入不合法（09 附錄 A 的 `VALIDATION_FAILED`）。

    與 FastAPI 的 `RequestValidationError` 分工：那個管**型別與格式**（pydantic 擋得
    住的），這個管**只有業務邏輯知道**的規則——1D-2 的第一個用例是「游標解不開」，
    而游標的合法性要拆開來看才知道。

    不用它的話，這類錯誤會掉進兜底的 `Exception` handler 變成 500——把 client 的
    錯誤記成我們的錯誤，而且 500 會進錯誤率告警。
    """

    code = ErrorCode.VALIDATION_FAILED


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


class UnsupportedMediaTypeError(DomainError):
    """→ 415。內容型別不在白名單內（10 §99：以 magic bytes 判定，不看副檔名）。

    **訊息裡帶「我們接受什麼」而不是「你送了什麼」**：後者要把偵測結果回吐給
    client，而偵測結果來自使用者送的位元組——那是一條把內部判定邏輯回聲出去的路。
    使用者需要的資訊也確實是前者。
    """

    code = ErrorCode.UNSUPPORTED_MEDIA_TYPE

    def __init__(self, *, accepted: tuple[str, ...]) -> None:
        super().__init__(
            f"檔案格式不支援，目前接受：{'、'.join(accepted)}",
            details={"accepted_media_types": list(accepted)},
        )


class UploadTooLargeError(DomainError):
    """→ 413。超過單請求上限（09 §3.1：>32MB 走分塊直傳）。

    ``details`` 帶上限值：client 才能在送出前先擋，而不是每次都靠伺服器回絕。
    """

    code = ErrorCode.UPLOAD_TOO_LARGE

    def __init__(self, *, limit_bytes: int) -> None:
        super().__init__(
            f"檔案超過單請求上限 {limit_bytes // (1024 * 1024)}MB",
            details={"limit_bytes": limit_bytes},
        )


class ProviderError(DomainError):
    """AI provider 呼叫失敗的共同基底（06 §4）。

    **可否重試由型別決定，不由訊息決定**：`retryable` 是 Gateway 唯一的判斷依據。
    寫成屬性而不是每次去看 HTTP 狀態碼，是因為那個判斷只該做一次——散在各處的話，
    總有一處會把「配額用盡」也拿去重試三次，而那三次都會失敗且都要付延遲。
    """

    code = ErrorCode.PROVIDER_UNAVAILABLE
    retryable = True


class ProviderTimeoutError(ProviderError):
    """→ 503。逾時（11 §4.1：所有對外呼叫必有 timeout）。

    可重試：慢的那一次不代表下一次也慢。上限由 Gateway 的重試次數控制。
    """


class ProviderUnavailableError(ProviderError):
    """→ 503。連不上、5xx、fallback 鏈耗盡。"""


class ProviderRateLimitedError(ProviderError):
    """→ 429。provider 端的頻率限制——退避後重試是正確處置。"""

    code = ErrorCode.RATE_LIMITED


class ProviderDimensionMismatchError(ProviderError):
    """→ 503。provider 回傳的向量維度與 `halfvec(N)` 欄位不合（1C-5）。

    **不可重試**：這是設定問題（adapter 沒送 `dimensions`、或 KB 指定了維度不同的
    模型），重試三次結果一樣，而每一次都要再付一批 embedding 的錢。

    刻意獨立成一個型別而不是沿用 `ProviderUnavailableError`：處置完全不同——後者是
    「等一下再試」，這個是「去把設定改對」，而 DLQ 上兩者長得一樣的話，維運分不出
    該修什麼。
    """

    code = ErrorCode.PROVIDER_UNAVAILABLE
    retryable = False


class ProviderAuthError(ProviderError):
    """→ 503。provider 拒絕我們的憑證（401／403）。

    **不可重試**：金鑰打錯、過期、被撤銷——重試三次還是同一個結果，只是把六分鐘的
    延遲加在一個確定的結論上，而那六分鐘裡 worker 一直被佔著。

    對外仍是 `PROVIDER_UNAVAILABLE`（503）而不是 401：這是**我們的**設定問題，租戶既
    看不懂也修不了。回 401 會讓他以為是自己的登入出了事。
    """

    code = ErrorCode.PROVIDER_UNAVAILABLE
    retryable = False


class ModelNotEnabledError(ProviderError):
    """→ 422。指定的模型未對本租戶啟用（或 provider 不認得它）。

    **不可重試**：模型不會因為再問一次就被啟用。這是設定問題，要讓它立刻浮上來。
    """

    code = ErrorCode.MODEL_NOT_ENABLED
    retryable = False

    def __init__(self, *, model: str) -> None:
        super().__init__(f"模型未啟用：{model}", details={"model": model})


class ExtractionFailedError(DomainError):
    """抽取階段失敗（08 §6）——不支援的型別、壞檔、毒檔、子行程異常。

    **第三方例外一律在 loader 邊界轉成這一個。** pdfplumber / python-docx 的例外型別
    會隨版本改變，而 08 §6 的重試判定（暫時性錯誤才重試，毒檔與 OOM 直接 failed）
    必須有穩定的依據。

    ``details["cause"]`` 是那個依據：``timeout`` / ``MemoryError`` / 原始例外的類別
    名 / ``unsupported_media_type``…。判定讀 ``cause``，訊息只給人看——措辭改動不該
    讓重試策略跟著變。
    """

    code = ErrorCode.ETL_FAILED

    def __init__(
        self,
        message: str,
        *,
        cause: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details={"cause": cause, **(details or {})})


class ObjectNotFoundError(DomainError):
    """→ 500。物件儲存裡找不到 DB 說存在的物件。

    這**不是** 404：使用者要的文件在 DB 裡確實存在，是我們這邊的兩份狀態不一致
    （物件被誤刪、bucket 換了、key 寫錯）。回 404 會讓使用者以為東西不見了而去
    重新上傳，真正的問題則沒有人知道。
    """

    code = ErrorCode.INTERNAL_ERROR


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

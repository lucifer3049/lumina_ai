"""RFC 9457 Problem Details —— 全 API 唯一的錯誤回應形狀（09 §1.3）。

放在 ``api/schemas/`` 而非 ``api/main.py``：這是**對外契約**，必須進 OpenAPI
讓前端 codegen 產得出型別（09 §4「錯誤 code 字典維護於單一 enum，文件自動
生成」；鐵則 10）。main.py 只負責組裝，形狀定義在這裡。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

PROBLEM_JSON = "application/problem+json"


class FieldError(BaseModel):
    """``errors[]`` 的元素——422 欄位級明細（09 附錄 A：VALIDATION_FAILED）。"""

    field: str
    message: str


class ProblemDetail(BaseModel):
    """RFC 9457 §3.1 的成員 + 本專案的 extension member（§3.2 允許）。"""

    type: str = Field(description="錯誤類型的 URI reference；無額外語意時為 about:blank")
    title: str = Field(description="錯誤類型的人類可讀摘要；同一 type 恆定")
    status: int = Field(description="HTTP 狀態碼，與回應行一致")
    detail: str = Field(description="這一次發生的情況；5xx 一律通用敘述，不含內部細節")
    request_id: str = Field(description="服務端產生的追蹤 id，與 X-Request-Id 標頭相同")
    code: str | None = Field(
        default=None,
        description="09 附錄 A 的穩定錯誤碼；client 唯一該用來分支的值。無對應碼時省略",
    )
    errors: list[FieldError] | None = Field(default=None, description="422 的欄位級明細")
    details: dict[str, Any] | None = Field(default=None, description="4xx 的補充資訊")


PROBLEM_SCHEMA_REF = "#/components/schemas/ProblemDetail"

# 直接寫 $ref 而不是給 ``model``：FastAPI 會把 model 的 schema 掛到
# ``route_response_media_type or "application/json"``（openapi/utils.py:499），
# 也就是掛在 application/json 底下——但錯誤回應的媒體型別是 problem+json，
# 掛錯地方等於 OpenAPI 說謊。components 的註冊改由 api/main.py 的
# ``_install_problem_schema`` 補上。
_PROBLEM_CONTENT: dict[str, Any] = {PROBLEM_JSON: {"schema": {"$ref": PROBLEM_SCHEMA_REF}}}

# 掛在 APIRouter 上讓所有端點共用（見 api/v1/*.py 的 router 宣告）。
# 逐端點宣告必然會漏，而漏掉不會有任何徵兆——tests/test_api_errors.py 的
# OpenAPI 測試會掃全部 operation 確認這四個狀態碼都在。
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"content": _PROBLEM_CONTENT, "description": "請求格式錯誤"},
    404: {"content": _PROBLEM_CONTENT, "description": "不存在或無權可見"},
    422: {"content": _PROBLEM_CONTENT, "description": "語意驗證失敗"},
    500: {"content": _PROBLEM_CONTENT, "description": "內部錯誤"},
}

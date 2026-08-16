"""串流對話的資料型別（06 §4、1D-3a）。

**上層只認得這裡的型別**，不知道 provider 是誰——與 `EmbedResult` 對 embedding 的關係
相同（鐵則 5）。差別在於對話的回應是**逐步交付**的，因此回傳不是一個結果物件，而是一串
delta。06 §4 把它定成五個型別：text / tool_call / usage / done / error。

**為什麼是五個而不是「一個帶 type 欄位的物件」**：呼叫端要對它們做完全不同的事（累加
文字、跑工具、記帳、收尾、報錯），而 `if delta.type == "..."` 的寫法讓型別檢查幫不上忙
——漏處理一種的症狀是那種事件安靜地被忽略（少一次記帳、少一個引用面板）。

**這個模組不 import Gateway 也不 import provider**：它是兩邊共用的字彙表。反過來的話
`ai.gateway` 與 `ai.gateway.providers` 會繞成循環 import。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from core.exceptions import ErrorCode

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChatTimeouts",
    "Delta",
    "DoneDelta",
    "ErrorDelta",
    "ProviderDelta",
    "TextDelta",
    "ToolCallDelta",
    "UsageDelta",
]

Role = Literal["system", "user", "assistant", "tool"]
ReasoningEffort = Literal["off", "low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """對話中的一則發言。

    ``role`` 的邊界同時是 injection 防護的前提（10 §5）：指令放 ``system``，外部資料
    （RAG context、工具輸出）只進 ``user`` 的 context 區塊。兩者混在同一個 role 裡的話，
    被污染的文件就能用「忽略以上指令」改寫我們的規則，而回答看起來完全正常。
    """

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """一次生成請求。**provider 無關**——各家的參數名由 adapter 翻譯。"""

    messages: Sequence[ChatMessage]
    model: str
    temperature: float | None = None
    max_output_tokens: int | None = None
    # 06 §4 的兩個介面預留欄位。功能屬 backlog（§3.5），但欄位現在就要在：
    # 之後才加等於改一次 `ChatRequest` 的形狀，而那時每一個呼叫端都要跟著動。
    # 不支援的 provider 靜默忽略（同 prompt cache 的降級模式）。
    reasoning_effort: ReasoningEffort = "off"
    response_format: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ChatTimeouts:
    """三層逾時（06 §4）。各自擋不同的故障，理由見 `app_settings` 的同名設定。"""

    connect_seconds: float
    ttft_seconds: float
    total_seconds: float

    @classmethod
    def from_settings(cls) -> ChatTimeouts:
        from config.settings.app_settings import get_app_settings

        settings = get_app_settings()
        return cls(
            connect_seconds=settings.ai_chat_connect_timeout_seconds,
            ttft_seconds=settings.ai_chat_ttft_timeout_seconds,
            total_seconds=settings.ai_chat_total_timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class TextDelta:
    """一段回答的文字。累加起來就是完整回應。"""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """一次工具呼叫。

    **參數是逐字元串流過來的**（`{"a` 、`":1}` 分成好幾個事件），組裝由 adapter 負責
    ——上層拿到的一律是完整的 JSON 字串。3A 才會有真的工具，但型別現在就要對：
    1D-4 的 SSE 事件對映與 05 §3.4 的 `tool_calls jb` 欄位都依它。
    """

    call_id: str
    name: str
    arguments: str
    status: Literal["running", "done", "failed"] = "running"


@dataclass(frozen=True, slots=True)
class UsageDelta:
    """這次呼叫的用量。**每一條串流都必定有一筆**，即使中途斷掉（1D-3a 的保證）。

    2A 的計費與成本控制拿它當原料，漏記等於租戶成本低估——而那種低估不會有人回報。
    """

    prompt_tokens: int
    completion_tokens: int
    model: str = ""
    provider: str = ""
    # **口徑：尚未計入 `completion_tokens` 的推理 token。**
    # 各家不同——OpenAI 的 `completion_tokens` 已經含推理（`completion_tokens_details`
    # 只是明細），有些 provider 則分開回報。正規化到同一個口徑是 adapter 的責任，
    # 因為只有它知道那一家怎麼算；口徑寫在這裡，是因為兩邊各自理解的話，錯的方向
    # （高估或低估）會隨 provider 而異，而兩種都不會有錯誤訊息。
    reasoning_tokens: int = 0
    # 用量是估的還是 provider 報的。2A 對帳時要分得出來——估算值對不上真帳單是正常，
    # 回報值對不上就是有人算錯了。
    estimated: bool = False

    @property
    def billable_output_tokens(self) -> int:
        """reasoning token **必須併入 output**（06 §4 明訂）。

        它按 output 計價，只是分開回報。不併的話帳面會比實際帳單少一截，而模型越新
        （推理越長）差得越多。
        """
        return self.completion_tokens + self.reasoning_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.billable_output_tokens


@dataclass(frozen=True, slots=True)
class DoneDelta:
    """**正常**講完了。中斷不會有這一筆（那是 `ErrorDelta`）。

    `finish_reason` 決定 1D-4 要不要把訊息標成 interrupted（`length` = 被 token 上限
    截斷），也決定 1D-5 要不要續跑工具迴圈（`tool_calls`）。
    """

    finish_reason: str


@dataclass(frozen=True, slots=True)
class ErrorDelta:
    """串流在**開始輸出之後**壞掉了。

    這個型別只有 Gateway 產得出來，provider 一律用例外表達失敗：還沒吐出第一個 token
    時失敗是例外（呼叫端還來得及決定 HTTP 狀態），之後才是 delta（HTTP 已經 200，
    09 §3.2）。兩者混成一種的話，1D-4 的端點不是在串流開始後還想改狀態碼，就是把
    「一個字都沒生出來」也回成 200 + 空串流。
    """

    code: ErrorCode = ErrorCode.STREAM_INTERRUPTED
    message: str = ""
    retryable: bool = True
    details: dict[str, object] = field(default_factory=dict)


# provider 吐得出來的（不含 error——失敗一律用例外表達，見 `ErrorDelta`）。
ProviderDelta = TextDelta | ToolCallDelta | UsageDelta | DoneDelta
# 呼叫端會看到的全部型別。
Delta = ProviderDelta | ErrorDelta

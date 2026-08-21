"""驗收：UsageService——usage_logs 的唯一寫入口（04 §8.2、05 §3.3，13 §4 工作包 2A-1）。

Gateway 保證 `usage` 事件恰一筆（1C-5），但**落地**至今不存在：chat 把數字寫進
`messages.usage` 的 jsonb 就結束了，embedding 的 tokens 只活在 task 的回傳值裡。
Quota 對帳（2A-2）與 Analytics（2A-3）都要從 usage_logs 讀，這一層是它們共同的地基。

寫入口只有一個 service 的理由同 PromptBuilder：散開的話，2A-2 的對帳要逐個呼叫端
確認「有沒有記、記的形狀一不一樣」，漏一個的症狀是對帳永遠差一塊。

三件事錯了都不會有例外：

1. **落地失敗打斷主流程**。usage 記錄是旁路：DB 抖一下不該讓使用者的回答消失、
   或讓一份文件的 embedding 白算。錯誤記 log，主流程繼續。
2. **成本用浮點數算**（見 test_model_pricing.py）。
3. **tenant 不進 context 就寫**。TenantScopedRepository 會 Fail Fast（鐵則 4），
   service 必須自己把 tenant_id 帶進 context——漏了的話每一次記錄都爆，
   而依第 1 條它不往外拋，症狀是 usage_logs 安靜地一直是空的。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from services.platform.usage import UsageEvent, UsageService


class _Recording:
    """假 repository：把 add 收到的東西錄下來。"""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, **fields: Any) -> None:
        self.rows.append(fields)


class _Exploding:
    def add(self, **fields: Any) -> None:
        raise RuntimeError("db is down")


_TENANT = uuid.UUID("11111111-1111-5111-8111-111111111100")


def _event(**overrides: Any) -> UsageEvent:
    defaults: dict[str, Any] = {
        "category": "llm",
        "model": "mock-chat",
        "prompt_tokens": 100_000,
        "completion_tokens": 50_000,
        "request_id": str(uuid.uuid4()),
    }
    defaults.update(overrides)
    return UsageEvent(**defaults)


class TestRecord:
    def test_a_row_reaches_the_repository(self) -> None:
        repo = _Recording()
        event = _event(
            user_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
        )

        UsageService(usage_logs=repo).record(_TENANT, event)

        assert len(repo.rows) == 1
        row = repo.rows[0]
        assert row["category"] == "llm"
        assert row["model"] == "mock-chat"
        assert row["prompt_tokens"] == 100_000
        assert row["completion_tokens"] == 50_000
        assert row["user_id"] == event.user_id
        assert row["conversation_id"] == event.conversation_id
        assert row["request_id"] == event.request_id

    def test_cost_is_computed_from_the_price_table(self) -> None:
        """計價在**寫入時**發生並落地。之後改價目表不會改歷史成本——帳是當時的價。"""
        repo = _Recording()

        UsageService(usage_logs=repo).record(_TENANT, _event())

        assert repo.rows[0]["cost"] == Decimal("0.045000")

    def test_an_unpriced_model_lands_with_cost_none(self) -> None:
        """缺價目照樣落地——tokens 是事實，價目是之後可補的詮釋。
        丟掉整列的話，補上價目也沒有東西可重算。"""
        repo = _Recording()

        UsageService(usage_logs=repo).record(_TENANT, _event(model="no-such-model"))

        assert len(repo.rows) == 1
        assert repo.rows[0]["cost"] is None

    def test_a_failing_repository_does_not_raise(self) -> None:
        """旁路原則：記錄失敗只能失去這一筆統計，不能失去使用者的回答。"""
        UsageService(usage_logs=_Exploding()).record(_TENANT, _event())

    def test_embedding_events_have_no_completion_tokens(self) -> None:
        repo = _Recording()

        UsageService(usage_logs=repo).record(
            _TENANT, _event(category="embedding", model="mock-embedding", completion_tokens=0)
        )

        row = repo.rows[0]
        assert row["category"] == "embedding"
        assert row["completion_tokens"] == 0
        assert row["cost"] == Decimal("0.002000")

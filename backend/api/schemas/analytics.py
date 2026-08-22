"""`/analytics/*` 的 I/O 契約（09 §2.6，2A-3）。

`cost` 是 Decimal：pydantic 會以字串序列化，client 端拿去再相加也不會產生
float 誤差（月報表「差幾分錢」是對帳最難查的一種差異）。
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel


class UsageBucketOut(BaseModel):
    """一個分組（key 依 group_by 而異：日期、user id、model 名或 category）。"""

    key: str
    requests: int
    prompt_tokens: int
    completion_tokens: int


class UsageOut(BaseModel):
    items: list[UsageBucketOut]


class CostBucketOut(BaseModel):
    key: str
    # None＝這一組全部缺價目（tokens 已入帳，補價目後重算 rollup 即可）。
    cost: Decimal | None


class CostsOut(BaseModel):
    items: list[CostBucketOut]

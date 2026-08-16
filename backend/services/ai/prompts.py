"""PromptService —— 版本解析 + 渲染（04 §5.3、1D-3b）。

04 §5.3 的介面：`render(prompt_key, version, variables) -> RenderedPrompt`、
`get_active_version(tenant, key)`。編排在這裡，演算法在 `ai/prompts/`、SQL 在
`repositories/ai.py`——與 `RetrievalService` 對 `rag/` 的關係完全相同。

**找不到模板一律 `NotFoundError`，不退回任何內建預設字串。** 退回的話，「模板沒種好」
會變成一個安靜的降級：系統照樣回答，只是用了一份沒有人 review 過的指令，而那份指令
不會出現在任何版本紀錄裡。回答品質變差是唯一的症狀。

**版本號要跟著結果一起回來**：1D-5 要把它寫進 `messages.prompt_version`（05 §3.4 的
生成快照）。回不出來的話那個欄位只能填 NULL，而「這個回答當時用了哪一版指令」就永遠
答不出來——那是 06 §1「版本化貫穿」的整個重點。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ai.prompts import render_template, validate_variables
from config.logging import get_logger
from core.exceptions import NotFoundError
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.ai import PromptRepository

logger = get_logger(__name__)

__all__ = ["SYSTEM_RAG_PROMPT_KEY", "PromptService", "RenderedPrompt"]

# 與 `apps/ai/migrations/0004_seed_rag_prompt.py` 種下的 key 一致。**兩份會漂**，
# 而漂掉的症狀是 render 時 NotFoundError——測試
# （tests/integration/test_prompt_repositories.py）拿這個常數去查那一列，對帳因此
# 發生在 CI 而不是在使用者按下送出的時候。
SYSTEM_RAG_PROMPT_KEY = "rag_answer"


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """渲染結果 —— **上層只認得這個型別**，不知道模板存在哪裡、哪一版生效。

    `system` 是要放進 `ChatMessage(role="system")` 的那段文字（1D-3a 的型別）。
    context 與問題**不在這裡**：它們是資料不是指令，由 `ai/prompts/` 的
    `build_context_block` 包成有邊界的一段放進 user 訊息（10 §5）。
    """

    key: str
    version: int
    system: str
    model_hint: dict[str, Any] = field(default_factory=dict)


class PromptService:
    def __init__(self, *, prompts: PromptRepository | None = None) -> None:
        self._prompts = prompts or PromptRepository()

    def render(
        self,
        tenant_id: uuid.UUID,
        *,
        key: str,
        variables: Mapping[str, Any] | None = None,
        version: int | None = None,
    ) -> RenderedPrompt:
        """解析出生效版本（或指定版本）並渲染。

        驗證在渲染**之前**：`StrictUndefined` 會在少變數時炸，但它說不出「你多給了一個
        打錯字的變數」——而那兩件事其實是同一個錯誤的兩面（見 `validate_variables`）。
        """
        payload = dict(variables or {})
        with tenant_context(tenant_id), unit_of_work():
            resolved = (
                self._prompts.version_for(key, version=version)
                if version is not None
                else self._prompts.active_version(key)
            )
            if resolved is None:
                raise NotFoundError("找不到可用的 prompt 模板")
            # 在交易內把要用的欄位取出來：離開 `unit_of_work` 之後再碰 model 屬性會
            # 觸發新的查詢，而那時已經沒有租戶 context 了（RLS 讀不到東西）。
            template = resolved.template
            schema = dict(resolved.variables_schema or {})
            model_hint = dict(resolved.model_hint or {})
            number = int(resolved.version)

        validate_variables(schema, payload)
        system = render_template(template, payload)

        logger.info("prompt_rendered", prompt_key=key, prompt_version=number)
        return RenderedPrompt(key=key, version=number, system=system, model_hint=model_hint)

    def get_active_version(self, tenant_id: uuid.UUID, key: str) -> int | None:
        """目前生效的版本號（04 §5.3 的第二個介面）。沒有生效版本時 None。"""
        with tenant_context(tenant_id), unit_of_work():
            resolved = self._prompts.active_version(key)
            return int(resolved.version) if resolved is not None else None

"""模型價目表與成本計算（05 §3.3、04 §8.2，2A-1）。

價目表住 `app_settings.ai_model_prices`（範圍偏離紀錄見該欄位的註解——
`model_configs` 落地時搬儲存位置，這裡的介面不變）。

兩條底線：

1. **錢一律 Decimal。** 浮點誤差在對帳時以「差幾分錢」出現，而那是對帳最難查的
   一種差異——它每一步看起來都對。
2. **缺價目是 None，不是 0。** 0 是「免費」，None 是「還不知道」；tokens 已落地，
   補上價目隨時可重算。壞條目只失去那一條（讀取容忍，同 `services/rag/params.py`
   的方向）：貼壞一個 model 的價，不該讓所有 model 的成本一起消失。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from config.logging import get_logger
from config.settings.app_settings import get_app_settings

logger = get_logger(__name__)

__all__ = ["ModelPrice", "compute_cost", "parse_model_prices"]

_ONE_MILLION = Decimal(1_000_000)
# usage_logs.cost 是 numeric(12,6)（05 §3.3）：捨入在寫入前就決定，
# 不留給 DB 安靜地截斷——兩邊的數字會對不上。
_COLUMN_SCALE = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """單位 USD / 1M tokens。"""

    prompt_usd_per_1m: Decimal
    completion_usd_per_1m: Decimal


def parse_model_prices(raw: str) -> dict[str, ModelPrice]:
    """`model:prompt/completion;...` → 價目表。

    人手在 .env 裡編輯的字串：空白與收尾分號屬正常形狀，靜默容忍；
    解析不出來或負數的**條目**丟掉並記 log——不是丟掉整份表。
    """
    prices: dict[str, ModelPrice] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        model, _, price_part = entry.rpartition(":")
        prompt_part, slash, completion_part = price_part.partition("/")
        if not model.strip() or not slash:
            logger.warning("model_price_entry_rejected", entry=entry, reason="format")
            continue
        try:
            price = ModelPrice(
                prompt_usd_per_1m=Decimal(prompt_part.strip()),
                completion_usd_per_1m=Decimal(completion_part.strip()),
            )
        except InvalidOperation:
            logger.warning("model_price_entry_rejected", entry=entry, reason="not_a_number")
            continue
        if price.prompt_usd_per_1m < 0 or price.completion_usd_per_1m < 0:
            # 負數單價沒有合法語意，只可能是打錯——照壞條目處理。
            logger.warning("model_price_entry_rejected", entry=entry, reason="negative")
            continue
        prices[model.strip()] = price
    return prices


def compute_cost(model: str, *, prompt_tokens: int, completion_tokens: int) -> Decimal | None:
    """這一次呼叫花了多少 USD；價目表沒有這個 model 時回 None。"""
    price = parse_model_prices(get_app_settings().ai_model_prices).get(model)
    if price is None:
        logger.warning("model_price_missing", model=model)
        return None
    cost = (
        Decimal(prompt_tokens) * price.prompt_usd_per_1m
        + Decimal(completion_tokens) * price.completion_usd_per_1m
    ) / _ONE_MILLION
    return cost.quantize(_COLUMN_SCALE)

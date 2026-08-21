"""驗收：模型價目表與成本計算（05 §3.3、04 §8.2，13 §4 工作包 2A-1）。

**範圍偏離（開工前標記，待人類裁決後生效）**：05 §3.3 把單價放在
`model_configs.pricing`，但那張表屬 Model 管理模組（04 §5.1），整個模組都還不存在。
先住 `app_settings` 的可調參數區（同 rag／chunk 參數的慣例，15 §4.1 的單一落點），
`model_configs` 落地時搬家——搬的是**儲存位置**，`compute_cost` 的介面不變。

格式沿用 `ai_chat_fallback_models` 的教訓：**不用 JSON**（.env 裡貼 JSON 很容易貼壞，
且錯的形狀會讓服務起不來），改用 `model:prompt/completion;...`，單位 USD / 1M tokens
（業界慣用的報價單位，抄價目表時不必換算）。

三件事錯了都不會有例外：

1. **缺價目時記 0**。0 是「免費」，會讓成本統計把那個模型的所有呼叫當成不用錢——
   正確的是 None（「還不知道」），之後補上價目可以用 tokens 重算。
2. **壞格式讓整份價目表失效**。一個 model 的價貼壞了，其他 model 的成本不該一起
   消失（讀取容忍，同 rag params 的方向）。
3. **浮點數算錢**。0.1 + 0.2 != 0.3 的那一類誤差在對帳時會以「差幾分錢」出現，
   而那正是對帳最難查的一種差異——一律 Decimal。
"""

from __future__ import annotations

from decimal import Decimal

from services.platform.pricing import ModelPrice, compute_cost, parse_model_prices

from config.settings.app_settings import get_app_settings


class TestDefaults:
    def test_mock_models_have_prices(self) -> None:
        """mock 兩個模型 day-1 就有價目。

        沒有的話，開發環境（永遠跑 mock）的 usage_logs 從第一天起 cost 全是 None，
        Analytics（2A-3）接上時看到的是一個「成本功能好像沒做」的畫面，而每一段
        程式其實都是對的。數字本身是隨意的起始點（mock 不用錢），可調。
        """
        settings = get_app_settings()
        prices = parse_model_prices(settings.ai_model_prices)

        assert prices["mock-chat"] == ModelPrice(
            prompt_usd_per_1m=Decimal("0.15"), completion_usd_per_1m=Decimal("0.60")
        )
        assert prices["mock-embedding"] == ModelPrice(
            prompt_usd_per_1m=Decimal("0.02"), completion_usd_per_1m=Decimal("0")
        )


class TestParsing:
    def test_a_normal_table_parses(self) -> None:
        prices = parse_model_prices("gpt-4o:2.50/10.00;text-embedding-3-small:0.02/0")

        assert prices["gpt-4o"] == ModelPrice(
            prompt_usd_per_1m=Decimal("2.50"), completion_usd_per_1m=Decimal("10.00")
        )
        assert prices["text-embedding-3-small"].completion_usd_per_1m == Decimal("0")

    def test_whitespace_and_trailing_semicolons_are_tolerated(self) -> None:
        """人手在 .env 裡編輯的字串，空白與收尾分號是必然出現的形狀。"""
        prices = parse_model_prices(" a:1/2 ; b:3/4 ; ")

        assert set(prices) == {"a", "b"}

    def test_a_bad_entry_does_not_take_down_the_rest(self) -> None:
        """壞一條只失去那一條（讀取容忍）。

        反過來的話，貼壞一個 model 的價，**所有** model 的成本同時變 None——而症狀
        出現在統計報表上，離貼壞的那一行隔了一整條 pipeline。
        """
        prices = parse_model_prices("good:1/2;bad:not-a-number;also-good:3/4")

        assert set(prices) == {"good", "also-good"}

    def test_negative_prices_are_rejected(self) -> None:
        """負數單價沒有合法語意，只可能是打錯——照壞條目處理。"""
        prices = parse_model_prices("weird:-1/2;fine:1/2")

        assert set(prices) == {"fine"}

    def test_an_empty_table_is_empty(self) -> None:
        assert parse_model_prices("") == {}


class TestComputeCost:
    def test_cost_is_tokens_times_price(self) -> None:
        """100k prompt × $0.15/1M + 50k completion × $0.60/1M = $0.045。"""
        cost = compute_cost("mock-chat", prompt_tokens=100_000, completion_tokens=50_000)

        assert cost == Decimal("0.045000")

    def test_the_result_is_decimal_not_float(self) -> None:
        cost = compute_cost("mock-chat", prompt_tokens=1_000_000, completion_tokens=0)

        assert isinstance(cost, Decimal)
        assert cost == Decimal("0.150000")

    def test_it_is_quantized_to_the_column_scale(self) -> None:
        """DB 欄位是 numeric(12,6)（05 §3.3）——超出的位數在**寫入前**就決定捨入，
        而不是讓 DB 安靜地截斷（兩邊的數字會對不上）。"""
        cost = compute_cost("mock-chat", prompt_tokens=10, completion_tokens=5)

        assert cost is not None
        assert cost == cost.quantize(Decimal("0.000001"))

    def test_an_unknown_model_costs_none(self) -> None:
        """None 是「還不知道」；0 是「不用錢」。混用的話補價目之後也分不出
        哪些列該重算。"""
        assert compute_cost("no-such-model", prompt_tokens=100, completion_tokens=100) is None

    def test_zero_tokens_cost_zero(self) -> None:
        assert compute_cost("mock-chat", prompt_tokens=0, completion_tokens=0) == Decimal("0")

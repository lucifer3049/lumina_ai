"""驗收：配額限額的解析與覆寫（04 §8.1、05 §3.3，13 §4 工作包 2A-2a）。

五種資源（04 §8.1）：`tokens_month`（token/月）、`messages_day`（訊息數/日）、
`documents`（文件數）、`storage_bytes`（儲存量）、`streams`（並發串流數）。

限額是**可調參數**（15 §4.1 的規矩）：plan 預設住 `app_settings`
（`quota_plan_free`，格式同價目表的 `resource:value;...`），租戶覆寫住
`tenant.settings["quota"]`——那正是 2C 統一設定畫面要寫的那一層。

三件事錯了都不會有例外：

1. **缺鍵當 0**。漏列一種資源的正確語意是「不限制」（None），不是「全面禁止」；
   反過來，新資源加進系統而 plan 字串沒跟上時，所有租戶會同時被鎖死。
2. **覆寫一鍵、其餘回到出廠**。租戶只調了 documents，token 限額卻被重設——
   同 rag params 的教訓，改的那一項確實生效了，所以沒有人會懷疑這裡。
3. **壞值讓解析整個爆掉**。tenant.settings 是使用者寫得到的 JSON（2C 之後更是），
   一個 `"documents": "很多"` 不該讓那個租戶從此不能上傳。
"""

from __future__ import annotations

from config.settings.app_settings import get_app_settings
from services.platform.quota import RESOURCES, resolve_limits


class TestDefaults:
    def test_free_plan_covers_every_resource(self) -> None:
        """免費方案五種資源都有上限（起始點數字，可調；tokens_month 的一百萬
        對齊 09 §1.3 的錯誤範例）。"""
        limits = resolve_limits("free")

        assert limits == {
            "tokens_month": 1_000_000,
            "messages_day": 200,
            "documents": 100,
            "storage_bytes": 1_073_741_824,
            "streams": 2,
        }

    def test_the_resource_list_is_pinned(self) -> None:
        """五種資源的名字是 API 契約的一部分（quota 端點與 429 details 都會帶）。"""
        assert set(RESOURCES) == {
            "tokens_month",
            "messages_day",
            "documents",
            "storage_bytes",
            "streams",
        }

    def test_the_token_reserve_estimate_exists(self) -> None:
        """chat 開場用「預留估計值」擋 token 配額（reserve/commit，04 §8.1）——
        它決定了「還剩一點點額度」的回合會不會被放行，是要能調的。"""
        assert get_app_settings().quota_token_reserve_estimate == 2000


class TestTokenReserve:
    """開場預留 = 設定值 **+ 問題本身的估計量**（`services/conversation/budget.py`）。

    設定值涵蓋的是「答案 + context」——它們的長度要到生成結束才知道，只能先估。但問題
    本身的長度**現在就知道**，而它可以差好幾個量級：一則貼滿的訊息（schema 上限
    32,000 字元）光是問題就三萬多 token。用一個固定的 2000 去擋線等於沒擋——真實用量
    要等收尾 commit 才追認，那時該擋的那幾則已經送出去、錢也花了。
    """

    def test_a_short_question_costs_about_the_flat_estimate(self) -> None:
        from services.conversation.budget import token_reserve_for

        flat = get_app_settings().quota_token_reserve_estimate

        assert flat <= token_reserve_for("你好") <= flat + 10

    def test_a_long_question_reserves_much_more(self) -> None:
        """三萬字的問題不該與兩個字的問題預留一樣多。"""
        from services.conversation.budget import token_reserve_for

        flat = get_app_settings().quota_token_reserve_estimate

        assert token_reserve_for("字" * 30_000) > flat * 10

    def test_it_never_goes_below_the_flat_estimate(self) -> None:
        """空問題走不到這裡（`start_turn` 先擋），但下界仍要是設定值——答案本身
        的成本與問題多短無關。"""
        from services.conversation.budget import token_reserve_for

        assert token_reserve_for("") == get_app_settings().quota_token_reserve_estimate


class TestPlanParsing:
    def test_an_unknown_plan_falls_back_to_free(self) -> None:
        """plan 欄位是自由文字（identity_tenant.plan），打錯字的租戶拿到的該是
        最保守的一組，而不是不設限。"""
        assert resolve_limits("no-such-plan") == resolve_limits("free")

    def test_a_missing_resource_means_unlimited(self) -> None:
        """plan 字串沒列的資源 = 不限制（缺鍵往寬鬆倒的理由見模組 docstring 第 1 條）。"""
        limits = resolve_limits("free", raw_plan="documents:5")

        assert limits["documents"] == 5
        assert limits["tokens_month"] is None
        assert limits["streams"] is None

    def test_a_bad_entry_does_not_take_down_the_rest(self) -> None:
        limits = resolve_limits("free", raw_plan="documents:abc;streams:3")

        assert limits["streams"] == 3
        assert limits["documents"] is None  # 壞條目被丟掉 → 該資源退回「不限制」


class TestTenantOverrides:
    def test_an_override_changes_only_that_key(self) -> None:
        limits = resolve_limits("free", overrides={"documents": 5})

        assert limits["documents"] == 5
        assert limits["tokens_month"] == 1_000_000, "沒被覆寫的鍵必須保持 plan 值"

    def test_a_bad_override_keeps_the_plan_value(self) -> None:
        limits = resolve_limits("free", overrides={"documents": "很多", "streams": 5})

        assert limits["documents"] == 100
        assert limits["streams"] == 5

    def test_an_override_may_lift_the_limit_entirely(self) -> None:
        """明確的 None＝「這個租戶不限制」——合約談出來的例外要表達得出來。"""
        limits = resolve_limits("free", overrides={"tokens_month": None})

        assert limits["tokens_month"] is None

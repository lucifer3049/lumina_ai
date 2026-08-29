"""驗收：三層覆寫的**中間層**（15 §4.1、09 §2.6，工作包 2C-1）。

覆寫順序自 1D-5 起就寫在 `services/rag/params.py` 的模組 docstring 上：

    系統預設（app_settings，env 可蓋）
      → 租戶設定（09 §2.6 的 `/settings`，**屬 2C，這一層還不存在**）
        → KB 覆寫（05 §3.2 的 `knowledge_base.config`）

中間那一層到這一包才存在。**它不生效與它不存在，在畫面上長得一模一樣**——後台填得
進去、讀得回來、看得見，而問答用的還是系統預設。那正是 15 §4.1 整條決定要防的症狀
（「後台改了沒有反應」），也是這一檔存在的全部理由。

四件錯了都不會有例外的事：

1. **層級順序反了**。KB 的值被租戶蓋掉——使用者為單一知識庫調的參數突然全庫一致，
   而他改的那一格看起來還在。
2. **中間層被跳過**。租戶設定寫得進 DB、讀得回畫面，就是不影響任何一次檢索。
3. **壞值一路退到系統預設**。租戶設了 `top_k=80`、KB 填錯成 `"很多"`，正確的行為是
   退回**租戶的 80**，不是跳過中間層退到系統的 40——那等於「填錯一個 KB 的值，整個
   租戶的設定跟著失效」。
4. **切塊那半邊沒接上**。檢索參數三層、切塊參數兩層的話，使用者會發現有些設定改得
   動、有些改不動，而兩者在同一個畫面上（2C-4 的統一設定畫面）。
"""

from __future__ import annotations

from typing import Any

import pytest

from config.settings.app_settings import get_app_settings
from services.knowledge.ingestion import _chunk_config_from
from services.rag.params import resolve_rag_params


def _settings() -> Any:
    return get_app_settings()


class TestRetrievalLayers:
    def test_no_overrides_means_system_defaults(self) -> None:
        params = resolve_rag_params(None, tenant_config=None)

        assert params.top_k == _settings().rag_top_k

    def test_a_tenant_override_takes_effect(self) -> None:
        """**這一條就是整包的目的。** 它紅的時候，症狀是「後台改了沒有反應」。"""
        params = resolve_rag_params(None, tenant_config={"retrieval": {"top_k": 12}})

        assert params.top_k == 12

    def test_a_kb_override_beats_the_tenant(self) -> None:
        """順序反了的話，使用者為單一 KB 調的值會被全租戶的設定蓋掉。"""
        params = resolve_rag_params(
            {"retrieval": {"top_k": 7}}, tenant_config={"retrieval": {"top_k": 12}}
        )

        assert params.top_k == 7

    def test_layers_are_merged_per_key_not_per_section(self) -> None:
        """KB 只覆寫一個鍵時，同一區的其他鍵仍要看得到租戶的值。

        整區取代的話，使用者在 KB 調了 `top_k`，租戶層的 `context_chunks` 會安靜地
        失效——而那一格在畫面上還顯示著租戶設的數字。
        """
        params = resolve_rag_params(
            {"retrieval": {"top_k": 7}},
            tenant_config={"retrieval": {"top_k": 12, "context_chunks": 3}},
        )

        assert (params.top_k, params.context_chunks) == (7, 3)

    def test_a_bad_kb_value_falls_back_to_the_tenant_not_the_system(self) -> None:
        """**退回的是下一層，不是最底層**（本檔第 3 條）。"""
        params = resolve_rag_params(
            {"retrieval": {"top_k": "很多"}}, tenant_config={"retrieval": {"top_k": 12}}
        )

        assert params.top_k == 12

    def test_a_wrongly_typed_tenant_value_falls_back_to_the_system(self) -> None:
        """讀取端一律容忍（15 §4.1）：壞值退回，不讓整個租戶問不了問題。"""
        params = resolve_rag_params(None, tenant_config={"retrieval": {"top_k": "很多"}})

        assert params.top_k == _settings().rag_top_k

    def test_an_out_of_range_tenant_value_is_clamped_not_dropped(self) -> None:
        """**範圍錯是夾制，型別錯才退回下一層**——這是 1D-5 起就定下的分界，多一層
        之後仍然成立。

        兩者混為一談的話會壞在相反的方向：把夾制改成退回，使用者填 `top_k=1000` 會
        安靜地變回系統預設（他以為自己調大了）；把退回改成夾制，`"很多"` 會被當成
        0 或 1，而那是拿一個沒有意義的數字去跑檢索。
        """
        params = resolve_rag_params(None, tenant_config={"retrieval": {"top_k": -5}})

        assert params.top_k == 1, "夾回下限，不是退回系統預設的 40"

    def test_a_tenant_section_that_is_not_an_object_is_ignored(self) -> None:
        """DB 裡本來就會有壞形狀（Django Admin 與 SQL 都寫得到）。"""
        params = resolve_rag_params(None, tenant_config={"retrieval": "top_k=10"})

        assert params.top_k == _settings().rag_top_k

    def test_an_unknown_tenant_key_does_not_break_the_rest(self) -> None:
        params = resolve_rag_params(
            None, tenant_config={"retrieval": {"top_kk": 99, "context_chunks": 3}}
        )

        assert params.context_chunks == 3


class TestChunkLayers:
    """切塊那半邊走同一條路——兩邊不一致的話，使用者會發現有些設定改得動、有些不會。"""

    def test_a_tenant_override_takes_effect(self) -> None:
        config = _chunk_config_from({}, tenant_config={"chunk": {"target_tokens": 300}})

        assert config.target_tokens == 300

    def test_a_kb_override_beats_the_tenant(self) -> None:
        config = _chunk_config_from(
            {"chunk": {"target_tokens": 256}}, tenant_config={"chunk": {"target_tokens": 300}}
        )

        assert config.target_tokens == 256

    def test_a_bad_kb_value_falls_back_to_the_tenant(self) -> None:
        config = _chunk_config_from(
            {"chunk": {"target_tokens": "五百"}}, tenant_config={"chunk": {"target_tokens": 300}}
        )

        assert config.target_tokens == 300

    def test_no_tenant_config_keeps_the_old_behaviour(self) -> None:
        """既有呼叫端不傳中間層時，行為必須與 2C-1 之前逐字相同。"""
        assert _chunk_config_from({}).target_tokens == _settings().chunk_target_tokens


class TestCrossFieldRulesSeeTheWholePicture:
    def test_overlap_is_checked_against_the_effective_target(self) -> None:
        """`overlap_tokens` 的上限是**同一區生效中的** `target_tokens`（`high_of`）。

        只看自己那一層的話：租戶設 target=128、KB 只設 overlap=200，overlap 會拿系統
        預設的 target 去比而被放行——切塊於是退化成幾乎不前進，每一塊都要付嵌入的錢。
        """
        config = _chunk_config_from(
            {"chunk": {"overlap_tokens": 200}}, tenant_config={"chunk": {"target_tokens": 128}}
        )

        assert config.target_tokens == 128
        assert config.overlap_tokens < config.target_tokens


class TestCallersWithoutTheMiddleLayer:
    """`tenant_config` 是選填——舊呼叫端一行都不必改（`params.py` 的承諾）。"""

    @pytest.mark.parametrize("kb_config", [None, {}, {"retrieval": {"top_k": 5}}])
    def test_the_signature_stays_backwards_compatible(self, kb_config: Any) -> None:
        assert resolve_rag_params(kb_config).top_k > 0

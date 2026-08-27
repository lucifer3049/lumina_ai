"""驗收：KB config 的**寫入端驗證**（09 §2.3、15 §4.1，13 §4 工作包 2B-5）。

15 §4.1 那一列寫的是「**寫入時驗證（2C）、讀取時容忍**」。讀取端那一半在 1D-5
（`services/rag/params.py`）與 1B（`_chunk_config_from`）已經有了，而寫入端這一半
到 2B-5 之前**根本不存在**——`knowledge_base.config` 沒有任何 API 寫得到，所以
「填錯了會怎樣」這個問題從來沒有被問過。

兩半的不對稱是刻意的，而且方向不能反過來：

- **讀取端容忍**：那條路跑在使用者按下送出之後的背景生成裡（或 ETL worker 裡）。
  一個打錯的欄位讓整輪失敗的話，使用者看到的是「一直出錯」，而不是「我的設定填錯
  了」。而且 DB 裡本來就可能有壞值——Django Admin 與 SQL 都寫得到，2C 之前的舊資料
  也沒經過任何驗證。讀取端改成 raise 等於讓那些 KB 從此問不了問題。
- **寫入端嚴格**：使用者正對著畫面按送出，這是唯一一個「告訴他填錯了」還來得及的
  時刻。放過去的話，他會看到 200、以為改好了，然後永遠不知道那個值沒有生效——
  「後台改了沒有反應」正是 15 §4.1 整條決定要防的症狀。

三件事錯了都不會有任何徵兆：

1. **打錯的鍵被安靜地存起來**。``{"retreival": {"top_k": 10}}`` 是合法的 JSON，存得
   進去、讀得回來、在設定畫面上看得見——只是永遠不生效。
2. **寫入端夾制而不是拒絕**。填 1000000 存成 200 並回 200，使用者以為自己設的是
   一百萬。讀取端夾制是對的（那時只能自救），寫入端夾制是騙人。
3. **兩端的上下限各寫一份**。今天一致，下次有人只改其中一邊——而那不會有任何測試
   紅燈，除非有一條測試把兩份釘在一起（`TestBoundsAreShared`）。
"""

from __future__ import annotations

from typing import Any

import pytest

from config.settings.app_settings import get_app_settings
from services.knowledge.ingestion import _chunk_config_from
from services.knowledge.kb_config import SECTIONS, KbConfigInvalidError, validate_kb_config
from services.rag.params import resolve_rag_params


def _fields(error: KbConfigInvalidError) -> list[str]:
    return [item["field"] for item in error.details["errors"]]


def _messages(error: KbConfigInvalidError) -> str:
    return " ".join(item["message"] for item in error.details["errors"])


def _rejects(config: dict[str, Any]) -> KbConfigInvalidError:
    with pytest.raises(KbConfigInvalidError) as excinfo:
        validate_kb_config(config)
    return excinfo.value


class TestAccepts:
    def test_empty_config_is_valid(self) -> None:
        """空 dict = 全部走系統預設（05 §81 的 `config jb`）。"""
        assert validate_kb_config({}) == {}

    def test_none_is_valid(self) -> None:
        assert validate_kb_config(None) == {}

    def test_a_valid_config_is_stored_verbatim(self) -> None:
        """**不補預設值、不夾制**：存進去的就是使用者填的那幾個鍵。

        寫入時把系統預設攤平寫進 config 的話，那個 KB 從此凍結在「今天的預設值」上
        ——之後調整系統預設，所有 KB 都不會跟著動，而使用者從來沒有設過那些值。
        三層覆寫（15 §4.1）的疊加是讀取端的事。
        """
        config = {"retrieval": {"top_k": 20}, "chunk": {"target_tokens": 800}}

        assert validate_kb_config(config) == config

    def test_one_section_alone_is_valid(self) -> None:
        assert validate_kb_config({"chunk": {"overlap_tokens": 32}}) == {
            "chunk": {"overlap_tokens": 32}
        }

    def test_an_empty_section_is_valid(self) -> None:
        """`{"retrieval": {}}` 是「這一區清空、回到系統預設」，不是錯誤。"""
        assert validate_kb_config({"retrieval": {}}) == {"retrieval": {}}

    def test_every_retrieval_mode_in_the_read_side_is_accepted(self) -> None:
        """四個模式（2B-3）：少放一個進白名單，使用者就設不了那一種，而錯誤訊息
        會說那是「不合法的值」——看起來像他打錯字。"""
        for mode in SECTIONS["retrieval"]["retrieval_mode"].choices or ():
            assert validate_kb_config({"retrieval": {"retrieval_mode": mode}})


class TestRejectsUnknownKeys:
    def test_an_unknown_section_is_rejected(self) -> None:
        """**這是這一包最主要的價值。** ``retreival`` 拼錯一個字母，讀取端會安靜地
        當作「這一區沒有設定」——設定畫面上看得到那個值，而它永遠不生效。"""
        error = _rejects({"retreival": {"top_k": 10}})

        assert _fields(error) == ["config.retreival"]
        # 訊息要列出有哪些區，否則使用者只知道「錯了」而不知道該打什麼。
        assert "retrieval" in _messages(error) and "chunk" in _messages(error)

    def test_an_unknown_key_inside_a_section_is_rejected(self) -> None:
        error = _rejects({"retrieval": {"top_kk": 10}})

        assert _fields(error) == ["config.retrieval.top_kk"]

    def test_a_key_from_the_wrong_section_is_rejected(self) -> None:
        """``{"retrieval": {"target_tokens": 512}}``——兩區的鍵不共用命名空間。
        放過去的話，切塊參數會被寫進檢索區，而兩邊都讀不到它。"""
        assert _fields(_rejects({"retrieval": {"target_tokens": 512}})) == [
            "config.retrieval.target_tokens"
        ]


class TestRejectsBadValues:
    def test_a_string_where_a_number_belongs_is_rejected(self) -> None:
        error = _rejects({"retrieval": {"top_k": "很多"}})

        assert _fields(error) == ["config.retrieval.top_k"]
        assert "整數" in _messages(error)

    def test_a_boolean_is_not_an_integer(self) -> None:
        """`bool` 是 `int` 的子類別，而 ``{"top_k": true}`` 的意思顯然不是 1
        （讀取端有同一條防線，見 `test_rag_params.py`）。"""
        assert _fields(_rejects({"retrieval": {"top_k": True}})) == ["config.retrieval.top_k"]

    def test_a_value_above_the_ceiling_is_rejected_not_clamped(self) -> None:
        """**寫入端不夾制。** 夾制等於「你填一百萬，我存 200，而且回你 200 OK」。

        讀取端夾制是對的（那時使用者不在，只能自救），寫入端夾制是騙人——他會在
        設定畫面上看到 200 而完全不知道發生過什麼。
        """
        ceiling = SECTIONS["retrieval"]["top_k"].high

        error = _rejects({"retrieval": {"top_k": 1_000_000}})

        assert _fields(error) == ["config.retrieval.top_k"]
        # 訊息要帶範圍：只說「超出範圍」的話，使用者得靠猜的才知道上限是多少。
        assert str(ceiling) in _messages(error)

    def test_a_value_below_the_floor_is_rejected(self) -> None:
        assert _fields(_rejects({"retrieval": {"top_k": 0}})) == ["config.retrieval.top_k"]

    def test_an_unknown_retrieval_mode_is_rejected_with_the_choices(self) -> None:
        error = _rejects({"retrieval": {"retrieval_mode": "hybrid+magic"}})

        assert _fields(error) == ["config.retrieval.retrieval_mode"]
        assert "hybrid+rerank" in _messages(error)

    def test_a_section_that_is_not_an_object_is_rejected(self) -> None:
        """``{"retrieval": "top_k=10"}``：讀取端當作「沒有這一區」而安靜地全走預設。"""
        assert _fields(_rejects({"retrieval": "top_k=10"})) == ["config.retrieval"]

    def test_a_config_that_is_not_an_object_is_rejected(self) -> None:
        assert _fields(_rejects(["top_k"])) == ["config"]  # type: ignore[arg-type]


class TestCrossFieldRules:
    def test_overlap_must_be_smaller_than_target(self) -> None:
        """``overlap_tokens >= target_tokens`` 代表「每一塊的開頭就是上一塊的全部」。

        切塊會退化成幾乎不前進——而它**不會報錯**，只會讓一份文件產出異常多的
        chunk，而每一塊都要付嵌入的錢。讀取端把它夾在 `target-1`（見
        `_chunk_config_from`），寫入端該做的是拒絕並說明。
        """
        error = _rejects({"chunk": {"target_tokens": 512, "overlap_tokens": 512}})

        assert _fields(error) == ["config.chunk.overlap_tokens"]
        assert "target_tokens" in _messages(error)

    def test_overlap_is_checked_against_the_given_target_not_the_default(self) -> None:
        """只改 overlap、target 沿用系統預設時，比的是**生效中的** target。

        拿 dataclass 的預設值去比的話，一個合法的組合會被擋下來（或反過來放過一個
        非法的），而兩種錯誤都只在特定的搭配下出現。
        """
        settings = get_app_settings()

        error = _rejects({"chunk": {"overlap_tokens": settings.chunk_target_tokens}})

        assert _fields(error) == ["config.chunk.overlap_tokens"]


class TestErrorReporting:
    def test_every_offending_key_is_reported_at_once(self) -> None:
        """**不是第一個錯就停。** 一次只講一個錯誤的話，使用者要來回試五次才知道
        自己填錯了五個地方——而每一次他都以為只剩最後一個。
        """
        error = _rejects(
            {
                "retrieval": {"top_k": "很多", "retrieval_mode": "magic"},
                "chunk": {"target_tokens": 999_999},
            }
        )

        assert set(_fields(error)) == {
            "config.retrieval.top_k",
            "config.retrieval.retrieval_mode",
            "config.chunk.target_tokens",
        }

    def test_the_field_path_matches_the_request_body(self) -> None:
        """欄位名是 ``config.<區>.<鍵>``——與 09 §1.3 的 ``errors[].field`` 一樣，
        指得回 client 送出去的那個位置。少了 ``config.`` 前綴的話，前端沒辦法把錯誤
        標在對的輸入框上（2C 的統一設定畫面就是靠這個定位）。"""
        assert _fields(_rejects({"retrieval": {"top_k": 0}})) == ["config.retrieval.top_k"]


class TestBoundsAreShared:
    """**寫入端與讀取端的上下限是同一份**（這一包把兩份合成一份）。

    `_chunk_config_from` 的 `_int` docstring 自 1B 起就寫著「與 `params.py` 的 `_int`
    是同一份邏輯的第二份……第三個呼叫端出現時再一起搬」。寫入端就是第三個。

    這幾條測試是那次合併的護欄：兩邊各自有測試不足以擋住漂移，因為兩邊各自都會綠。
    """

    def test_the_read_side_clamps_to_exactly_the_written_ceiling(self) -> None:
        spec = SECTIONS["retrieval"]["top_k"]
        assert spec.high is not None

        params = resolve_rag_params({"retrieval": {"top_k": spec.high + 1}})

        assert params.top_k == spec.high

    def test_the_chunk_read_side_clamps_to_exactly_the_written_ceiling(self) -> None:
        spec = SECTIONS["chunk"]["target_tokens"]
        assert spec.high is not None

        config = _chunk_config_from({"chunk": {"target_tokens": spec.high + 1}})

        assert config.target_tokens == spec.high

    def test_every_spec_names_a_real_setting(self) -> None:
        """預設值一律指向 `app_settings` 的欄位（15 §4.1）。

        指到一個不存在的欄位名時，症狀是「那個參數永遠回同一個值」——而那個值看起來
        很正常，因為它就是別人的預設值。
        """
        settings = get_app_settings()

        for section, specs in SECTIONS.items():
            for key, spec in specs.items():
                assert hasattr(settings, spec.default_attr), f"{section}.{key}"

    def test_the_read_side_knows_every_key_the_write_side_accepts(self) -> None:
        """寫得進去卻讀不出來的鍵是最糟的一種：它通過驗證、存進 DB、在設定畫面上
        顯示，然後完全不生效。"""
        params = resolve_rag_params(
            {"retrieval": dict.fromkeys(SECTIONS["retrieval"], None)}  # 值不合法，看的是鍵
        )

        for key in SECTIONS["retrieval"]:
            assert hasattr(params, key), key


class TestReadSideStaysTolerant:
    """**這一包不准把讀取端改嚴。**

    寫入端擋住的是「今天以後填進來的東西」；DB 裡已經有的壞值（Django Admin、SQL、
    2C 之前寫進去的）不會因此消失。讀取端改成 raise 的話，那些 KB 會從此問不了問題，
    而錯誤發生在背景生成裡——使用者看到的只有「一直出錯」。
    """

    def test_a_bad_stored_value_still_falls_back_instead_of_raising(self) -> None:
        settings = get_app_settings()

        assert resolve_rag_params({"retrieval": {"top_k": "很多"}}).top_k == settings.rag_top_k

    def test_an_unknown_stored_key_is_ignored_by_the_read_side(self) -> None:
        """寫入端拒絕未知鍵，讀取端忽略它——兩者不衝突：一個是「不准再寫進來」，
        另一個是「既然已經在裡面了，不要因此壞掉」。"""
        assert resolve_rag_params({"retrieval": {"top_kk": 10}}).top_k > 0

    def test_a_bad_stored_chunk_value_still_falls_back(self) -> None:
        settings = get_app_settings()

        config = _chunk_config_from({"chunk": {"target_tokens": "五百"}})

        assert config.target_tokens == settings.chunk_target_tokens

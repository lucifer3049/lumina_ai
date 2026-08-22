"""驗收：切塊參數的解析（06 §2.1「策略與參數由 KB 決定」、15 §4.1 的可調參數集中）。

與 `test_rag_params.py` 是同一個原則的另一半：**寫入時驗證、讀取時容忍**。KB config
是使用者寫得到的 JSON，而這條路跑在 worker 裡——一個打錯的值不該讓那份文件失敗。

原本 `_chunk_config_from` 對它直接呼叫 `int()`：``{"chunk": {"target_tokens": "五百"}}``
會丟 ValueError → Celery 重試三次 → 文件標 failed 且 ``retryable=True``。使用者看到的
是「處理失敗，可以重跑」（重跑當然也一樣失敗），而真正的原因是他自己填的那個字串。
檢索參數那半邊做了這件事，切塊這半邊漏了。
"""

from __future__ import annotations

from typing import Any

from config.settings.app_settings import get_app_settings
from services.knowledge.ingestion import _chunk_config_from


def _config(**chunk: Any) -> dict[str, Any]:
    return {"chunk": chunk}


class TestDefaults:
    def test_no_section_gives_the_system_defaults(self) -> None:
        """預設值住在 `app_settings`（15 §4.1），不是 `ChunkConfig` 的欄位預設——
        後者留在 dataclass 上的話，「後台調得到」對切塊這半邊就是假的。"""
        settings = get_app_settings()

        config = _chunk_config_from({})

        assert config.target_tokens == settings.chunk_target_tokens
        assert config.overlap_tokens == settings.chunk_overlap_tokens

    def test_a_non_dict_section_is_ignored(self) -> None:
        assert _chunk_config_from({"chunk": "target=500"}).target_tokens > 0


class TestOverride:
    def test_a_valid_value_wins(self) -> None:
        assert _chunk_config_from(_config(target_tokens=800)).target_tokens == 800

    def test_one_key_overrides_only_itself(self) -> None:
        settings = get_app_settings()

        config = _chunk_config_from(_config(target_tokens=800))

        assert config.overlap_tokens == settings.chunk_overlap_tokens


class TestBadValues:
    def test_a_string_falls_back_instead_of_exploding(self) -> None:
        """**這是修掉的那個 bug。** ValueError 發生在 worker 裡，而使用者看到的是
        一份 `retryable=True` 的失敗文件——那個字看起來像基礎設施故障。"""
        settings = get_app_settings()

        config = _chunk_config_from(_config(target_tokens="五百"))

        assert config.target_tokens == settings.chunk_target_tokens

    def test_a_bool_is_not_an_int(self) -> None:
        """`bool` 是 `int` 的子類別，而 ``{"target_tokens": true}`` 顯然不是要 1
        ——那會讓每一個字各自成為一個 chunk。"""
        settings = get_app_settings()

        assert (
            _chunk_config_from(_config(target_tokens=True)).target_tokens
            == settings.chunk_target_tokens
        )

    def test_a_null_falls_back(self) -> None:
        settings = get_app_settings()

        assert (
            _chunk_config_from(_config(overlap_tokens=None)).overlap_tokens
            == settings.chunk_overlap_tokens
        )


class TestClamping:
    def test_a_tiny_target_is_raised_to_the_floor(self) -> None:
        """target=1 會讓一份文件炸出上萬個 chunk，而每一個都是一次真的嵌入呼叫。"""
        assert _chunk_config_from(_config(target_tokens=1)).target_tokens >= 64

    def test_a_huge_target_is_capped(self) -> None:
        """超過 embedding 模型的輸入上限時，症狀是整份文件永遠失敗。"""
        assert _chunk_config_from(_config(target_tokens=10_000_000)).target_tokens <= 4_000

    def test_overlap_never_reaches_target(self) -> None:
        """overlap ≥ target 代表「每一塊的開頭就是上一塊的全部」——切塊幾乎不前進，
        而它不會報錯，只會產出異常多的 chunk（真的錢）。"""
        config = _chunk_config_from(_config(target_tokens=200, overlap_tokens=500))

        assert config.overlap_tokens < config.target_tokens

    def test_a_negative_overlap_becomes_zero(self) -> None:
        assert _chunk_config_from(_config(overlap_tokens=-10)).overlap_tokens == 0

"""KB config 的形狀 —— **寫入端驗證與讀取端解析共用的唯一一份宣告**（15 §4.1、2B-5）。

15 §4.1 的那一列寫的是「**寫入時驗證、讀取時容忍**」。兩半的方向刻意相反：

- **讀取端容忍**（`services/rag/params.py`、`_chunk_config_from`）：那條路跑在使用者
  按下送出之後的背景生成裡，或跑在 ETL worker 裡。一個打錯的欄位讓整輪失敗的話，
  使用者看到的是「一直出錯」而不是「我的設定填錯了」。而且 DB 裡本來就會有壞值——
  Django Admin 與 SQL 都寫得到，2C 之前寫進去的資料也沒經過任何驗證。
- **寫入端嚴格**（本檔的 :func:`validate_kb_config`）：使用者正對著畫面按送出，這是
  唯一一個「告訴他填錯了」還來得及的時刻。放過去的話他會看到 200、以為改好了，然後
  永遠不知道那個值沒有生效——「後台改了沒有反應」正是 15 §4.1 整條決定要防的症狀。

**上下限只准有一份。** `_chunk_config_from` 的 `_int` docstring 自 1B 起就寫著：

    與 `services/rag/params.py` 的 `_int` 是同一份邏輯的第二份……第三個呼叫端出現時
    再一起搬。

寫入端就是第三個。兩份上下限漂掉時**兩邊各自都會綠**（各有各的測試），而症狀是
「後台填得進去的值，實際跑起來被夾成別的」——沒有任何錯誤訊息。合成一份之後，
`tests/unit/test_kb_config_write.py::TestBoundsAreShared` 把兩端釘在一起。

**這一層不是「系統預設值住的地方」**：預設值住在 `config/settings/app_settings.py`
（env 可蓋），這裡只記「哪個參數對應哪個設定欄位」（`default_attr`）。指到一個不存在
的欄位時，症狀是那個參數永遠回同一個值，而那個值看起來很正常——因為它就是別人的
預設值。`TestBoundsAreShared::test_every_spec_names_a_real_setting` 擋這件事。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from config.settings.app_settings import get_app_settings
from core.exceptions import ValidationFailedError

__all__ = [
    "MAX_TOP_K",
    "RETRIEVAL_MODES",
    "SECTIONS",
    "KbConfigInvalidError",
    "ParamSpec",
    "layers_of",
    "read_param",
    "section_of",
    "validate_kb_config",
    "validate_param_sections",
]

# **保護 DB 的硬上限，不是使用者可調的東西**（15 §4.1 的例外條款）。`top_k` 直接進
# SQL 的 LIMIT，而沒有上限時一個 `top_k=1000000` 不會失敗，只會讓 pgvector 把整個 KB
# 掃出來排序——那幾秒對**所有租戶**都很慢。
MAX_TOP_K = 200
_MAX_CONTEXT_CHUNKS = 50
_MAX_TOKEN_BUDGET = 100_000
# RRF 的 k 沒有物理上限，但大到某個程度之後每一段的貢獻都趨近 1/k——名次完全失去
# 意義，而結果看起來只是「排序怪怪的」。
_MAX_RRF_K = 1000
_MAX_HISTORY_TURNS = 10
# 切塊參數的硬上下限——**保護的是我們自己，不是使用者可調的東西**（同 `MAX_TOP_K`）。
# target 太小會讓一份文件炸出上萬個 chunk（每一個都要嵌入，是真的錢）；太大則超過
# embedding 模型的輸入上限，症狀是整份文件永遠失敗。
_MIN_TARGET_TOKENS = 64
_MAX_TARGET_TOKENS = 4_000

# 檢索模式的白名單。**不是從 `app_settings` 的 Literal 推導**：那份型別是給環境變數
# 用的，而這裡的輸入是使用者寫得到的 KB config。
#
# 四格而不是三格（2B-3）：沒有 `vector+rerank` 的話，`hybrid+rerank` 贏了也分不出是
# rerank 的功勞還是 hybrid 的——而 2B-4 的實測正是「贏的是 rerank，hybrid 的邊際
# 貢獻是零」，那個歸因問題不是假想的。
RETRIEVAL_MODES = ("vector", "vector+rerank", "hybrid", "hybrid+rerank")


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """一個可調參數的完整宣告：預設值從哪來、值長什麼樣、範圍到哪裡。

    `default_attr` 是 `app_settings` 上的欄位名——**不是預設值本身**。把數字寫在這裡
    的話，env 覆寫就對這個參數失效，而那不會有任何徵兆（15 §4.1）。
    """

    default_attr: str
    kind: Literal["int", "float", "choice"]
    low: int | float | None = None
    high: int | float | None = None
    choices: tuple[str, ...] | None = None
    # 上限改由**同一區的另一個鍵**決定（值 - 1）。目前只有 `overlap_tokens`：
    # overlap ≥ target 代表「每一塊的開頭就是上一塊的全部」，切塊會退化成幾乎不
    # 前進——而它不會報錯，只會讓一份文件產出異常多的 chunk，每一塊都要付嵌入的錢。
    high_of: str | None = None


SECTIONS: Mapping[str, Mapping[str, ParamSpec]] = {
    # 檢索側（1D-5 起）。鍵名與 `RagParams` 的欄位名**逐字相同**——不同的話，
    # 「寫得進去卻讀不出來」會變成可能，而那種鍵會通過驗證、存進 DB、在設定畫面上
    # 顯示，然後完全不生效。
    "retrieval": {
        "top_k": ParamSpec("rag_top_k", "int", low=1, high=MAX_TOP_K),
        "fts_top_k": ParamSpec("rag_fts_top_k", "int", low=1, high=MAX_TOP_K),
        "rrf_k": ParamSpec("rag_rrf_k", "int", low=1, high=_MAX_RRF_K),
        "hybrid_candidates": ParamSpec("rag_hybrid_candidates", "int", low=1, high=MAX_TOP_K),
        "retrieval_mode": ParamSpec("rag_retrieval_mode", "choice", choices=RETRIEVAL_MODES),
        "rerank_threshold": ParamSpec("rag_rerank_threshold", "float", low=0.0, high=1.0),
        "context_chunks": ParamSpec("rag_context_chunks", "int", low=1, high=_MAX_CONTEXT_CHUNKS),
        "context_token_budget": ParamSpec(
            "rag_context_token_budget", "int", low=1, high=_MAX_TOKEN_BUDGET
        ),
        "min_score_ratio": ParamSpec("rag_min_score_ratio", "float", low=0.0, high=1.0),
        "query_history_turns": ParamSpec(
            "rag_query_history_turns", "int", low=0, high=_MAX_HISTORY_TURNS
        ),
    },
    # 切塊側（1B）。同一個慣例（`config.chunk`）——兩套命名的話，2C 的設定畫面要為
    # 每個功能各寫一次讀寫邏輯。
    "chunk": {
        "target_tokens": ParamSpec(
            "chunk_target_tokens", "int", low=_MIN_TARGET_TOKENS, high=_MAX_TARGET_TOKENS
        ),
        "overlap_tokens": ParamSpec("chunk_overlap_tokens", "int", low=0, high_of="target_tokens"),
    },
}


class KbConfigInvalidError(ValidationFailedError):
    """→ 422 + `errors[]`（09 §1.3、附錄 A）。

    **逐欄位回報，不是回一句「設定不合法」**：2C 的統一設定畫面要把錯誤標在對的
    輸入框上，而它只有 `field` 這條線索。欄位名用 ``config.<區>.<鍵>``，與 FastAPI
    的 `loc` 同一個形狀——client 那邊因此不必為這一種錯誤寫第二套解析。
    """

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("知識庫設定不合法", details={"errors": errors})


def validate_kb_config(config: Any) -> dict[str, Any]:
    """KB config 的寫入端驗證（2B-5）。前綴固定 ``config.``——`KnowledgeBaseOut` 的
    `config` 欄位就叫這個名字，而 2C-4 的畫面靠 `field` 標到對的輸入框。"""
    return validate_param_sections(config, prefix="config", error=KbConfigInvalidError)


def validate_param_sections(
    config: Any,
    *,
    prefix: str,
    error: Callable[[list[dict[str, str]]], Exception],
    also_known: Sequence[str] = (),
) -> dict[str, Any]:
    """使用者送來的 config → 原樣的 config，或 :class:`KbConfigInvalidError`。

    **不補預設值、不夾制。** 兩者都是讀取端的職責：

    - 補預設值的話，那個 KB 從此凍結在「今天的預設值」上——之後調整系統預設，所有
      KB 都不會跟著動，而使用者從來沒有設過那些值。
    - 夾制的話，使用者填 1000000、我們存 200、然後回他 200 OK。讀取端夾制是對的
      （那時使用者不在，只能自救），寫入端夾制是騙人。

    **一次回報全部的錯**：一次只講一個的話，使用者要來回試五次才知道自己填錯了五個
    地方——而每一次他都以為只剩最後一個。
    """
    if config is None:
        return {}
    if not isinstance(config, Mapping):
        raise error([_error(prefix, "必須是物件")])
    known = (*SECTIONS, *also_known)

    errors: list[dict[str, str]] = []
    cleaned: dict[str, Any] = {}
    for name, raw in config.items():
        specs = SECTIONS.get(str(name))
        if specs is None:
            # 打錯一個字母（`retreival`）是這一層最主要要擋的東西：它是合法的 JSON，
            # 存得進去、讀得回來、在設定畫面上看得見——只是永遠不生效。
            errors.append(
                _error(f"{prefix}.{name}", f"不是可設定的區塊；可用的有 {'、'.join(known)}")
            )
            continue
        if not isinstance(raw, Mapping):
            errors.append(_error(f"{prefix}.{name}", "必須是物件"))
            continue
        cleaned[str(name)] = dict(raw)
        errors += _validate_section(str(name), raw, specs, prefix=prefix)

    if errors:
        raise error(errors)
    return cleaned


def _validate_section(
    name: str, section: Mapping[Any, Any], specs: Mapping[str, ParamSpec], *, prefix: str
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for key, value in section.items():
        field = f"{prefix}.{name}.{key}"
        spec = specs.get(str(key))
        if spec is None:
            # 兩區的鍵不共用命名空間：`{"retrieval": {"target_tokens": 512}}` 放過去
            # 的話，切塊參數會被寫進檢索區，而**兩邊都讀不到它**。
            errors.append(_error(field, f"不是可設定的參數；可用的有 {_listed(specs)}"))
            continue
        message = _reject_reason(spec, value, section=section, specs=specs)
        if message is not None:
            errors.append(_error(field, message))
    return errors


def _reject_reason(
    spec: ParamSpec, value: Any, *, section: Mapping[Any, Any], specs: Mapping[str, ParamSpec]
) -> str | None:
    """不合法的理由；合法時回 `None`。**訊息一律帶上允許的範圍或選項**——只說
    「超出範圍」的話，使用者得靠猜的才知道上限是多少。"""
    if spec.kind == "choice":
        if isinstance(value, str) and value in (spec.choices or ()):
            return None
        return f"必須是下列之一：{'、'.join(spec.choices or ())}（收到 {value!r}）"

    # `bool` 是 `int` 的子類別，而 `{"top_k": true}` 的意思顯然不是 `top_k=1`。
    if isinstance(value, bool):
        return "必須是整數" if spec.kind == "int" else "必須是數字"
    if spec.kind == "int" and not isinstance(value, int):
        return f"必須是整數（收到 {value!r}）"
    if spec.kind == "float" and not isinstance(value, int | float):
        return f"必須是數字（收到 {value!r}）"

    high = _ceiling(spec, sections=[section], specs=specs)
    if spec.low is not None and value < spec.low:
        return f"必須介於 {_shown(spec.low)} 與 {_shown(high)} 之間（收到 {value!r}）"
    if high is not None and value > high:
        if spec.high_of is not None:
            # 跨欄位的規則要說得出**跟誰比**：只回一個數字的話，使用者調了 target
            # 之後會發現「同一個 overlap 這次可以、上次不行」。
            return f"必須小於 {spec.high_of}（目前是 {high + 1}，收到 {value!r}）"
        return f"必須介於 {_shown(spec.low)} 與 {_shown(high)} 之間（收到 {value!r}）"
    return None


def read_param(
    specs: Mapping[str, ParamSpec],
    key: str,
    sections: Sequence[Mapping[str, Any]],
    settings: Any,
    *,
    on_rejected: Callable[[str, Any], None],
) -> Any:
    """**讀取端**：由具體到一般逐層找 → 夾在上下限內的值；找不到就用系統預設。

    `sections` 的順序是**最具體的在前**（KB → 租戶）。15 §4.1 的覆寫順序只該有一份
    實作，而它就在這個迴圈裡——散進三個呼叫端的話，「KB 蓋租戶」與「租戶蓋系統」會
    在某一處被寫反，而那不會有任何錯誤訊息。

    **壞值退回的是下一層，不是最底層**：租戶設了 80、KB 填錯成 `"很多"` 時該用 80。
    一路退到系統預設等於「填錯一個 KB 的值，整個租戶的設定跟著失效」。

    與 :func:`validate_kb_config` 共用同一份 `specs`，這正是這個模組存在的理由。

    **不 raise**：這條路跑在使用者按下送出之後的背景生成（或 ETL worker）裡，例外會
    讓整輪失敗，而使用者看到的是「一直出錯」而不是「我的設定填錯了」。`on_rejected`
    留下線索——不記的話，一個打錯的值會安靜地表現成「後台改了沒有反應」。
    """
    spec = specs[key]
    for section in sections:
        if key not in section:
            continue
        value = section[key]
        accepted = _accept(spec, value, sections=sections, specs=specs, settings=settings)
        if accepted is None:
            on_rejected(key, value)
            continue
        return accepted
    return getattr(settings, spec.default_attr)


def _accept(
    spec: ParamSpec,
    value: Any,
    *,
    sections: Sequence[Mapping[str, Any]],
    specs: Mapping[str, ParamSpec],
    settings: Any,
) -> Any | None:
    """合法就回**夾制後**的值，不合法回 `None`（由呼叫端決定退到哪一層）。"""
    if spec.kind == "choice":
        return value if isinstance(value, str) and value in (spec.choices or ()) else None

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if spec.kind == "int" and not isinstance(value, int):
        return None

    number = float(value) if spec.kind == "float" else int(value)
    high = _ceiling(spec, sections=sections, specs=specs, settings=settings)
    if spec.low is not None:
        number = max(spec.low, number)
    if high is not None:
        number = min(high, number)
    return float(number) if spec.kind == "float" else int(number)


def section_of(config: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    """config 裡的某一區。不是物件（或不存在）時回空的——**讀取端不因此失敗**。"""
    raw = (config or {}).get(name)
    return raw if isinstance(raw, Mapping) else {}


def layers_of(name: str, *configs: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """把幾份 config 的同一區收成「由具體到一般」的層序（2C-1）。

    呼叫端寫 ``layers_of("retrieval", kb_config, tenant_config)``——順序即優先序，
    而不是物件的形狀決定的。傳 `None` 或該區不是物件時那一層就不存在（讀取端不因此
    失敗，同 `section_of`）。
    """
    return [section_of(config, name) for config in configs if section_of(config, name)]


def _ceiling(
    spec: ParamSpec,
    *,
    sections: Sequence[Mapping[str, Any]],
    specs: Mapping[str, ParamSpec],
    settings: Any | None = None,
) -> int | float | None:
    """這個參數此刻的上限。跨欄位的（`high_of`）跟著**生效中的**那個值走。

    拿系統預設去比的話，一個合法的組合會被擋下來（或反過來放過一個非法的），而兩種
    錯誤都只在特定的搭配下出現——使用者只會覺得「有時候可以有時候不行」。

    讀取端（`settings` 有給）比的是**夾制之後**的值：`{"target_tokens": 10}` 會先被
    夾回下限 64，overlap 的上限就該是 63 而不是 9。寫入端比的是原值——那時 target
    自己的越界會由它自己那一條規則回報，這裡不重複講同一個錯。
    """
    if spec.high_of is None:
        return spec.high

    other = specs[spec.high_of]
    if settings is not None:
        effective = read_param(specs, spec.high_of, sections, settings, on_rejected=_ignored)
    else:
        raw = next(
            (s[spec.high_of] for s in sections if spec.high_of in s),
            None,
        )
        effective = (
            raw
            if isinstance(raw, int) and not isinstance(raw, bool)
            else getattr(get_app_settings(), other.default_attr)
        )
    return max(int(effective) - 1, 0)


def _ignored(key: str, value: object) -> None:
    """解析「別人的上限」時不記 warning——那個鍵自己被讀到時已經記過一次了。"""


def _error(field: str, message: str) -> dict[str, str]:
    return {"field": field, "message": message}


def _listed(keys: Mapping[str, Any]) -> str:
    return "、".join(keys)


def _shown(value: int | float | None) -> str:
    """整數不要印成 `40.0`——使用者填的是 40，訊息裡出現 40.0 會讓人以為型別也錯了。"""
    if value is None:
        return "—"
    return str(int(value)) if float(value).is_integer() else str(value)

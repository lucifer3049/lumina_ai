"""驗收：分層依賴的約束宣告完整（02 §3、CLAUDE.md 鐵則 2）。

**這個檔案不檢查有沒有違規**——那是 `lint-imports`（import-linter）的工作，
在 `make lint` 與 CI 各跑一次。這裡檢查的是**約束本身有沒有漏宣告**。

差別很重要：import-linter 只驗證「已宣告的規則沒有被違反」。若某天新增了
`ai/` 套件卻忘了替它寫 contract，import-linter 會安靜地全綠——因為沒有規則可違反。
腐化就是這樣發生的：不是有人打穿了分層，而是新的一層從來沒被納管。

因此本檔的斷言是：**repo 內每個實際存在的層級套件，都必須被至少一條 contract 提到**，
且 02 §3 列出的每條規則都有對應宣告。新增套件後沒補 contract 會在這裡紅燈。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = BACKEND_ROOT / "pyproject.toml"

# 不受分層約束的頂層套件：
# - config：組態與進程入口，本來就要 import 所有層來組裝
# - tests：測試可以看見任何東西
# - loadtest：壓測腳本，非產品程式碼
UNCONSTRAINED = {"config", "tests", "loadtest"}

# 02 §3 的規則 3：下層不知道上層。套件尚未建立時不強求（Phase 1C 起陸續出現），
# 但**一旦目錄存在就必須有 contract**——這是本檔存在的意義。
LOWER_LAYERS = ("ai", "rag", "etl", "tool")


def _load_contracts() -> list[dict[str, Any]]:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    importlinter = config.get("tool", {}).get("importlinter", {})
    assert importlinter, "pyproject.toml 缺少 [tool.importlinter] 設定"
    contracts = importlinter.get("contracts", [])
    assert contracts, "[tool.importlinter] 沒有宣告任何 contract"
    return list(contracts)


def _existing_layer_packages() -> set[str]:
    """backend/ 下實際存在的 Python 套件（有 __init__.py 的目錄）。"""
    return {
        path.name
        for path in BACKEND_ROOT.iterdir()
        if path.is_dir() and (path / "__init__.py").exists() and path.name not in UNCONSTRAINED
    }


def _modules_mentioned(contract: dict[str, Any]) -> set[str]:
    """contract 中提到的所有頂層模組名（不分 source / forbidden / layers）。"""
    mentioned: set[str] = set()
    for value in contract.values():
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, list):
            candidates = [item for item in value if isinstance(item, str)]
        else:
            continue
        for item in candidates:
            # layers contract 的元素可能寫成 "api" 或 "api.v1"；只取頂層
            mentioned.add(item.split(".", 1)[0].strip())
    return mentioned


def _forbidden_pairs() -> set[tuple[str, str]]:
    """所有「source 不可 import forbidden」的組合（頂層名）。"""
    pairs: set[tuple[str, str]] = set()
    for contract in _load_contracts():
        if contract.get("type") != "forbidden":
            continue
        sources = [m.split(".", 1)[0] for m in contract.get("source_modules", [])]
        forbidden = [m.split(".", 1)[0] for m in contract.get("forbidden_modules", [])]
        pairs.update((source, target) for source in sources for target in forbidden)
    return pairs


def test_every_existing_layer_package_is_covered() -> None:
    """新增了層級套件卻沒寫 contract 會在此失敗（import-linter 本身不會發現）。"""
    mentioned: set[str] = set()
    for contract in _load_contracts():
        mentioned |= _modules_mentioned(contract)

    uncovered = _existing_layer_packages() - mentioned
    assert not uncovered, (
        f"這些套件沒有出現在任何 import-linter contract 中：{sorted(uncovered)}"
        "——新增一層就要同時宣告它的依賴邊界（02 §3）"
    )


def test_controller_cannot_reach_orm() -> None:
    """鐵則 2 / 3：api 不可 import repositories、apps。"""
    pairs = _forbidden_pairs()
    for target in ("repositories", "apps"):
        assert ("api", target) in pairs, f"缺少 contract：api 不可 import {target}"


def test_services_cannot_reach_models_directly() -> None:
    """鐵則 2：services 只能經 repository 取資料。"""
    assert ("services", "apps") in _forbidden_pairs(), "缺少 contract：services 不可 import apps"


def test_lower_layers_do_not_know_upper_layers() -> None:
    """ai / rag / etl / tool 不可 import api、services——目錄存在才要求。"""
    existing = _existing_layer_packages()
    pairs = _forbidden_pairs()

    for layer in LOWER_LAYERS:
        if layer not in existing:
            continue
        for upper in ("api", "services"):
            assert (layer, upper) in pairs, f"缺少 contract：{layer} 不可 import {upper}"


def test_common_is_a_leaf() -> None:
    """common 是純函式庫，不可 import 任何其他層——目錄存在才要求。"""
    existing = _existing_layer_packages()
    if "common" not in existing:
        return

    pairs = _forbidden_pairs()
    others = existing - {"common"}
    missing = {layer for layer in others if ("common", layer) not in pairs}
    assert not missing, f"缺少 contract：common 不可 import {sorted(missing)}"

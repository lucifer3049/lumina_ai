"""驗收：OpenAPI 契約以檔案形式進版控，且與 app 的實際 schema 不漂移（09 §4、03 §3.1）。

**為什麼契約要落成一個檔案**：03 §3.1 的 codegen 是「schema → 前端 typed client」，
若 schema 只存在於執行中的行程（`/openapi.json`），那 CI 每次產生前端 client 都得先起
一套 API，而且沒有任何一份可比對的基準——09 §4 要求的 oasdiff（breaking change 審查）
也無從做起。把 schema 匯出成 repo 根的 `openapi.json` 並進版控之後，契約變更會出現在
diff 裡、被 review，前端 codegen 也只讀檔案。

**本檔與 `make openapi-check` 的分工**：這裡驗的是「committed 契約 == 現在的 app」，
純記憶體比對，unit 層就跑得動、不需要 node 也不需要起服務；`make openapi-check`
驗的是再往下一段——「generated client == committed 契約」，那需要 pnpm，在 CI 前端 job。
兩段都要有，中間任一段沒對上，前端型別就會與後端行為脫節而毫無徵兆。

型式沿用 `test_migrations_in_sync.py`：漂移類的守門一律做成「重新產生一次再比對」，
不比對人手維護的清單。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
EXPORT_SCRIPT = BACKEND_ROOT / "scripts" / "export_openapi.py"
CONTRACT = REPO_ROOT / "openapi.json"

REGENERATE_HINT = "請跑 `make openapi`（並把改動一起提交，那是 API 契約變更）"


@pytest.fixture(scope="module")
def export_module() -> ModuleType:
    """載入匯出腳本。

    走檔案路徑而非 `import scripts.export_openapi`：`scripts/` 刻意**不是** Python
    套件（沒有 `__init__.py`），否則它會被 `test_layer_contracts.py` 當成新的層級
    套件，要求替一支建置腳本宣告 import-linter contract。

    測試不自己寫一份序列化：格式（縮排、鍵排序、結尾換行）只要有兩份實作就會漂，
    而漂掉的症狀是「本機 make openapi 之後 CI 說有 diff」，兩邊都無法信任。
    """
    assert EXPORT_SCRIPT.exists(), f"缺少匯出腳本：{EXPORT_SCRIPT.relative_to(REPO_ROOT)}"
    spec = importlib.util.spec_from_file_location("_export_openapi", EXPORT_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 註冊進 sys.modules：腳本內若有 dataclass / pickle 之類需要回查模組的行為，
    # 未註冊時會以難懂的 KeyError 失敗。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    assert CONTRACT.exists(), (
        f"缺少 API 契約檔 {CONTRACT.relative_to(REPO_ROOT)}——前端 codegen 的輸入。{REGENERATE_HINT}"
    )
    loaded = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_contract_matches_the_current_app(export_module: ModuleType) -> None:
    """committed 契約 == 現在的 app 產出的 schema。

    這是本檔的主斷言：改了端點、schema、operation_id 卻忘了重新匯出時紅燈。
    比對的是**序列化後的文字**而非 dict——鍵順序與縮排也屬於契約檔的一部分，
    只比 dict 的話，格式漂掉會讓 `make openapi-check` 在 CI 上以 git diff 失敗，
    而本層卻是綠的，紅燈位置指向錯誤的方向。
    """
    rendered = export_module.render(export_module.build_schema())
    assert rendered == CONTRACT.read_text(encoding="utf-8"), (
        f"{CONTRACT.name} 與目前的 FastAPI schema 不一致——{REGENERATE_HINT}"
    )


def test_rendering_is_deterministic(export_module: ModuleType) -> None:
    """同一份 schema 連續匯出兩次必須逐字相同。

    不決定性（鍵順序隨 dict 插入順序、無結尾換行）會讓契約檔在無實質變更時也產生
    diff，於是 review 的人開始習慣性忽略它——契約治理就是這樣失效的。
    """
    schema = export_module.build_schema()
    first = export_module.render(schema)
    second = export_module.render(export_module.build_schema())

    assert first == second, "匯出結果不決定性（鍵順序或格式隨執行而變）"
    assert first.endswith("\n"), "契約檔缺少結尾換行——git diff 會標 `\\ No newline at end of file`"
    assert json.loads(first) == schema, "render 改變了 schema 的內容，不只是格式"


def test_export_excludes_the_spike_surface(
    export_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契約不得含 spike 面，且不隨環境變數而變。

    `/api/v1/spike/*` 與 `X-Tenant-Id` 取租戶違反 ADR-002，只在壓測時開啟
    （見 `api/main.py`）。匯出若讀 `AppSettings`，那在跑過壓測的機器上匯出，
    未認證端點就會**進到公開契約與前端 client 裡**——而且 diff 看起來像是正常的
    新端點。因此匯出一律顯式關閉旗標。
    """
    monkeypatch.setenv("ENABLE_SPIKE_ENDPOINTS", "true")

    paths = export_module.build_schema().get("paths", {})

    spike_paths = [path for path in paths if "spike" in path]
    assert not spike_paths, (
        f"契約含 spike 端點 {spike_paths}——匯出必須顯式傳 enable_spike_endpoints=False，"
        "不可讀環境設定（ADR-002）"
    )


def test_every_v1_router_is_mounted() -> None:
    """``api/v1/`` 下每一個 router 模組的路由都必須真的掛在 app 上。

    **為什麼上面的漂移比對抓不到這件事**：``test_contract_matches_the_current_app``
    驗的是「契約 == app」，而漏了 ``include_router`` 時**兩邊都是空的**——契約沒有
    那個端點、app 也沒有，於是 diff 乾淨、CI 全綠，只有前端拿到一個少了端點的
    typed client。``make openapi-check`` 同理，它比的是 export 與 codegen 兩份產物，
    共同的上游若整段缺失，兩份會一致地缺。

    Phase 0 的契約 ``paths`` 目前確實是空的（唯一的 router 是 spike，而契約刻意
    不收錄它，見 ``test_export_excludes_the_spike_surface``），所以這裡比對的是
    **開啟旗標**的 app：驗的是「router 有被掛上」，與契約要不要收錄它是兩件事。
    1A 加入第一個正式 router 後，這條會在忘記 ``include_router`` 時紅燈。
    """
    from api.main import create_app

    paths = create_app(enable_spike_endpoints=True).openapi()["paths"]
    modules = sorted(
        path.stem for path in (BACKEND_ROOT / "api" / "v1").glob("*.py") if path.stem != "__init__"
    )

    assert modules, "api/v1/ 下沒有任何模組——這條守門會變成空轉"

    missing: list[str] = []
    for name in modules:
        router = getattr(importlib.import_module(f"api.v1.{name}"), "router", None)
        if router is None:
            continue
        # getattr：BaseRoute 不保證有 path（Mount / WebSocketRoute 就沒有）。
        declared = [getattr(route, "path", None) for route in router.routes]
        missing += [
            f"api.v1.{name}:{path}"
            for path in declared
            if isinstance(path, str) and not any(mounted.endswith(path) for mounted in paths)
        ]

    assert not missing, f"以下路由未被 create_app 掛載（漏了 include_router）：{missing}"


def test_contract_carries_the_error_schema(contract: dict[str, Any]) -> None:
    """`ProblemDetail` 必須在 components 裡。

    錯誤格式是 09 §1.3 的契約，前端 `api/client.ts` 的錯誤正規化要靠它產出型別。
    `api/main.py` 的 `_install_problem_schema()` 是手動補進去的（`ERROR_RESPONSES`
    只寫 `$ref`），一旦那段被改掉，契約會安靜地少掉錯誤型別。
    """
    schemas = contract.get("components", {}).get("schemas", {})
    assert "ProblemDetail" in schemas, (
        "契約缺少 ProblemDetail——前端會失去錯誤型別（09 §1.3、api/main.py _install_problem_schema）"
    )


def test_contract_is_openapi_3(contract: dict[str, Any]) -> None:
    """openapi-typescript 只吃 OpenAPI 3.x；版本欄消失時前端 codegen 的錯誤訊息很難懂。"""
    version = contract.get("openapi", "")
    assert isinstance(version, str) and version.startswith("3."), (
        f"契約的 openapi 版本欄不是 3.x：{version!r}"
    )

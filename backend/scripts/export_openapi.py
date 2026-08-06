"""把 FastAPI 的 OpenAPI schema 匯出成 repo 根的 ``openapi.json``（09 §4、03 §3.1）。

**為什麼要落成檔案**：schema 只活在執行中的行程時，前端 codegen 每次都得先起一套
API，而且沒有「上一版契約」可比——09 §4 要求的 breaking change 審查（oasdiff）
就無從做起。落檔之後契約變更會出現在 PR diff 裡被 review，CI 產生前端 client
也只需要讀檔。FastAPI 仍是唯一產生者，這個檔案是它的快照，不是第二份真相；
兩者是否一致由 ``tests/unit/test_openapi_export.py`` 每次 unit 測試比對。

**這不是套件**：`scripts/` 刻意沒有 ``__init__.py``。有的話
``tests/unit/test_layer_contracts.py`` 會把它當成新的層級套件，要求替一支建置腳本
宣告 import-linter contract——那條規則管的是架構分層，不是工具。

用法（一律經 make，路徑與環境變數都在那裡處理好）::

    make openapi
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
CONTRACT = REPO_ROOT / "openapi.json"

# 直接執行時 sys.path[0] 是 scripts/，`import api.main` 會失敗。
# pyproject 的 `pythonpath = ["."]` 只對 pytest 生效。
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def build_schema() -> dict[str, Any]:
    """產生對外契約的 schema。

    ``enable_spike_endpoints=False`` 是**顯式**傳入而非讓它讀設定：``/api/v1/spike/*``
    與 ``X-Tenant-Id`` 取租戶違反 ADR-002，只在壓測時開啟。若這裡讀
    ``AppSettings``，在跑過壓測的機器上匯出就會把未認證端點寫進公開契約與前端
    client，而 diff 看起來只像是多了幾個正常端點。
    """
    from api.main import create_app

    return create_app(enable_spike_endpoints=False).openapi()


def render(schema: dict[str, Any]) -> str:
    """序列化成契約檔的文字。

    ``sort_keys``：FastAPI 產生的 dict 順序跟著程式碼的宣告順序走，把端點上下搬動
    就會產生一份沒有實質變更的巨大 diff——那種 diff 幾次之後就沒有人會看了。
    ``ensure_ascii=False``：欄位說明是中文，轉成 ``\\uXXXX`` 等於契約檔不可讀。
    結尾換行：少了它 git 會標 ``\\ No newline at end of file``。
    """
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    # newline="\n" 顯式指定：Windows 上文字模式會把 \n 轉成 \r\n，於是同一份 schema
    # 在不同平台匯出得到不同位元組，`make openapi-check` 會報漂移卻找不到原因。
    # （本專案統一在 WSL2 開發，這行是防禦性的。）
    CONTRACT.write_text(render(build_schema()), encoding="utf-8", newline="\n")
    print(f"已寫入 {CONTRACT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""測試設定（A 組正確性測試）。

與 dev 的唯一差異：**直連 PostgreSQL(15432)，繞過 PgBouncer**。
原因：pytest-django 需要 CREATE/DROP DATABASE，而 PgBouncer transaction mode
綁定固定 dbname，無法代理這類操作。

`CONN_MAX_AGE` 在測試中固定為 0：測試要的是可預期的連線行為，不是效能。
"""

from __future__ import annotations

import os
from typing import Any, cast

from .base import *  # noqa: F403
from .base import DATABASES

DATABASES["default"]["HOST"] = os.environ.get("DB_DIRECT_HOST", "127.0.0.1")
DATABASES["default"]["PORT"] = os.environ.get("DB_DIRECT_PORT", "15432")
DATABASES["default"]["CONN_MAX_AGE"] = 0

# `admin` alias（base.py）本來就直連，這裡不需要改。兩條連線的差別只在**角色**：
# default = 應用角色（受 RLS 管），admin = schema owner（建 test database 與跑
# migration）。繞不繞 PgBouncer 不影響 RLS——policy 認的是連線角色，不是路徑。

# ── statement_timeout：測試連線放寬（1D-2）──────────────────────
#
# **正式環境的 5 秒（11 §4.1）不動**，這裡只放寬測試連線。理由是測試會做一件正式
# 環境永遠不會做的事：`transaction=True` 的測試在每條結束後 `TRUNCATE` 全部的表。
#
# 1D-1 之後那個動作變重了——`conversation_message` 是分區表，TRUNCATE 要一次鎖住
# 父表加 12 個分區。六個 xdist worker 同時做這件事時，偶爾會超過 5 秒，而失敗的
# 症狀完全不指向這裡：
#
#     canceling statement due to statement timeout
#     Database test_lumina_gw2 couldn't be flushed
#     → 下一條測試 duplicate key value violates "identity_tenant_pkey"
#
# flush 失敗代表**上一條測試的資料留在庫裡**，於是下一條插同一個租戶 id 就撞唯一鍵
# ——受害者是隨機的（誰排在後面誰倒楣），而單獨跑任何一條都會過。1D-2 加了 55 條
# 會寫訊息的測試之後，這個競態從「幾乎不發生」變成約每兩次跑一次。
#
# **走 startup 參數（`-c`）可行的唯一原因是測試直連 PostgreSQL**：base.py 的
# docstring 說明它對正式環境無效，因為 PgBouncer 的 `ignore_startup_parameters`
# 會靜默丟棄 `options`——而測試繞過 PgBouncer（見本檔開頭）。
#
# 仍然給一個上限而不是 0：真的卡住的查詢還是要失敗，只是不要把 TRUNCATE 誤殺。
TEST_STATEMENT_TIMEOUT = "60s"

# **只覆寫 `default`（應用角色），不碰 `admin`。** owner 角色刻意沒有
# statement_timeout（13 §3.1 的 1A-P3：5s 會砍掉大表的 AddIndexConcurrently 與 HNSW
# 建索引，而那時 schema 已是半套），`tests/integration/test_db_roles.py` 有測試釘住。
# 對 admin 設值會讓那條測試紅——而且它本來就不需要：owner 沒有上限，flush 不會超時。
# `cast` 是為了 mypy：`DATABASES` 的值型別是 `object`（Django 的設定字典沒有精確
# 型別），直接對它索引賦值會被判成不支援的操作。
_default_options = cast("dict[str, Any]", DATABASES["default"].setdefault("OPTIONS", {}))
_default_options["options"] = f"-c statement_timeout={TEST_STATEMENT_TIMEOUT}"

# ── AI provider：測試一律 mock（CLAUDE.md 鐵則）────────────────────
#
# **這不是預設值，是強制值。** `make test` 帶 `--env-file ../.env`，而 `AppSettings`
# 讀的就是那些環境變數——`.env` 裡設了真 provider 與金鑰時，整個測試套件會開始打真的
# API。1C-5 實測撞到：加了 Gemini 金鑰之後，`make test` 有 10 條紅燈，而它們是**真的
# 發出去的網路請求**（回 422 model-not-enabled，因為測試用的模型名是 mock-embedding）。
#
# 三個代價，每一個都足以單獨否決它：測試會花錢；會因為別人的服務中斷而紅；而
# MockProvider 的決定性（同樣的文字永遠得到同樣的向量）正是檢索測試的前提，真 provider
# 沒有那個性質。
#
# 金鑰一併清掉：即使哪天有人繞過上面那行，也沒有東西可以拿去花。
#
# **設成空字串而不是 `pop`**：`AppSettings` 的 `env_file` 直接指向 repo 根的 `.env`，
# 所以把環境變數刪掉之後，pydantic 還是會從那個檔案讀到金鑰（實測如此）。環境變數的
# 優先序高於 `.env`，因此覆寫成空字串才蓋得掉——而「空字串等同沒有金鑰」由
# `ai/gateway/__init__.py` 的建構守門保證。
os.environ["AI_EMBEDDING_PROVIDER"] = "mock"
os.environ["AI_EMBEDDING_MODEL"] = "mock-embedding"
os.environ["AI_EMBEDDING_API_KEY"] = ""

# 這個模組在 `django.setup()` 期被 import，通常早於第一次 `get_app_settings()`——但
# 「通常」不夠：只要有任何一條 import 路徑先讀過設定，上面三行就白寫了，而症狀是
# 測試偶爾會打真 API。清一次快取讓它與時序無關。
from .app_settings import get_app_settings  # noqa: E402

get_app_settings.cache_clear()

"""測試設定（A 組正確性測試）。

與 dev 的唯一差異：**直連 PostgreSQL(15432)，繞過 PgBouncer**。
原因：pytest-django 需要 CREATE/DROP DATABASE，而 PgBouncer transaction mode
綁定固定 dbname，無法代理這類操作。

`CONN_MAX_AGE` 在測試中固定為 0：測試要的是可預期的連線行為，不是效能。
"""

from __future__ import annotations

import os

from .base import *  # noqa: F403
from .base import DATABASES

DATABASES["default"]["HOST"] = os.environ.get("DB_DIRECT_HOST", "127.0.0.1")
DATABASES["default"]["PORT"] = os.environ.get("DB_DIRECT_PORT", "15432")
DATABASES["default"]["CONN_MAX_AGE"] = 0

# `admin` alias（base.py）本來就直連，這裡不需要改。兩條連線的差別只在**角色**：
# default = 應用角色（受 RLS 管），admin = schema owner（建 test database 與跑
# migration）。繞不繞 PgBouncer 不影響 RLS——policy 認的是連線角色，不是路徑。

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

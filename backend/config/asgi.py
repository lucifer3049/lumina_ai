"""ASGI 入口 —— FastAPI app（ADR-001）。

**本檔的 import 順序是架構約束，不是風格偏好。**

``django.setup()`` 必須在此檔 **import 期**完成，且早於任何會觸及 model 的
import。原因（ADR-001 修訂紀錄 2026-08-01）：

    建立 FastAPI app 物件需先 import router → services → repositories → models，
    而 Django model 在 **import 當下**就會存取 app registry。若把 ``django.setup()``
    放在 FastAPI lifespan，執行時序在整份 import 完成之後——太晚，直接
    ``ImproperlyConfigured``。

因此下方 ``from api.main import create_app`` 帶著 ``# noqa: E402``：這不是
偷懶繞過 lint，是這個檔案唯一正確的寫法。**移動它會讓服務起不來。**
"""

from __future__ import annotations

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

# ── 分隔線：以下 import 會連帶載入 model，必須排在 django.setup() 之後 ──
from api.main import create_app  # noqa: E402

app = create_app()

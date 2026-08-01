"""開發 / spike 壓測用設定。"""

from __future__ import annotations

from .base import *  # noqa: F403

DEBUG = False
"""刻意維持 False。

``DEBUG=True`` 會讓 Django 把每一條 SQL 累積在 ``connection.queries`` 裡不釋放
——壓測時這是記憶體洩漏，也會扭曲吞吐數字。
"""

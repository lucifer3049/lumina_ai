#!/usr/bin/env python
"""Django 管理指令入口（migration / shell / seed）。

ADR-001：Django 只當 ORM + Migration 引擎，不對外提供 HTTP。此檔僅供
維運與開發指令使用；對外服務入口是 ``config/asgi.py``。
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    from django.core.management import execute_from_command_line

    # settings.LOGGING_CONFIG = None：CLI 這條路徑同樣要自己設定日誌，
    # 否則 migration / seed 期間的 log 退回 stdlib 預設（純文字、只剩 WARNING 以上）。
    from config.logging import configure_logging

    configure_logging()
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()

"""部署用設定 —— backend image 的預設 settings module。

**存在的理由不是「現在有東西要設」，是「不要讓部署落到 dev 上」。**
``config/asgi.py`` 以 ``os.environ.setdefault`` 決定 settings module，預設值是
``config.settings.dev``。image 若不顯式指定，容器就會以 dev 設定運行——而那不會
有任何徵兆：dev 目前恰好等同 base，所以行為一致、測試全綠。危險在於 dev.py 是
「可以放開發便利設定」的地方（它的 docstring 正在討論 ``DEBUG``），一旦有人在
那裡加了東西，就會同時進到部署環境。

因此 backend/Dockerfile 顯式設 ``DJANGO_SETTINGS_MODULE=config.settings.prod``，
由 tests/unit/test_logging.py 對帳。K8s / compose 需要別的組態時覆寫該環境變數。

目前與 base 無差異：Phase 0 沒有 prod 專屬設定，而先塞一份猜測值比留空更糟
（沒人知道那些值的依據是什麼）。prod 專屬項隨部署工作包進來。
"""

from __future__ import annotations

from .base import *  # noqa: F403

"""驗收：`ALLOWED_HOSTS` 走環境變數（`config/settings/base.py`；二次架構審計 F-09）。

原本是寫死的 `["*"]`。**目前沒有讀者**——Django 不對外服務 HTTP（鐵則 1），沒有
ROOT_URLCONF、沒有 MIDDLEWARE，`CommonMiddleware` 的 Host 驗證從未執行。

那正是它危險的地方：2C 計畫掛上 Django Admin，而那個 PR 會在 urls.py 與 MIDDLEWARE
上——**不會有人想到來 review 這一行**。萬用字元於是安靜地生效，Host 標頭偽造
（快取污染、密碼重設信指向攻擊者的網域）就此打開。

本檔不 import settings 模組（它在 import 期就讀環境變數，且已被 pytest 載入過），
改為直接測那個函式——它是這條規則的全部實作。
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.settings.base import _allowed_hosts


class TestExplicitValue:
    def test_a_comma_separated_list_is_split(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "lumina.example.com,api.lumina.example.com")

        assert _allowed_hosts() == ["lumina.example.com", "api.lumina.example.com"]

    def test_whitespace_and_empty_entries_are_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`a, b,` 這種手寫值很常見。空字串進了清單會讓 Django 比對永遠不中，
        而症狀是全部請求 400——看起來像應用壞了。"""
        monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", " lumina.example.com , , api.example.com ")

        assert _allowed_hosts() == ["lumina.example.com", "api.example.com"]


class TestProductionRefusesToGuess:
    def test_production_without_the_variable_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**不給「安全的預設」**：漏設的部署會照常起來，而漏設的那一刻正是這條
        防線唯一有用的時候（同 `_required_env` 的理由）。"""
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.delenv("DJANGO_ALLOWED_HOSTS", raising=False)

        with pytest.raises(ImproperlyConfigured, match="DJANGO_ALLOWED_HOSTS"):
            _allowed_hosts()

    def test_an_unset_environment_counts_as_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """漏設 ENVIRONMENT 時落在嚴格那邊（同 app_settings 的 `environment` 預設）。"""
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("DJANGO_ALLOWED_HOSTS", raising=False)

        with pytest.raises(ImproperlyConfigured):
            _allowed_hosts()


class TestDevelopmentDefault:
    def test_development_falls_back_to_loopback_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Django 對 `DEBUG=False` 一律要求非空清單，而本 repo 的 DEBUG 恆為 False
        （見 dev.py）——開發環境需要一個能動的預設，但那個預設不是 `*`。"""
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.delenv("DJANGO_ALLOWED_HOSTS", raising=False)

        hosts = _allowed_hosts()

        assert "*" not in hosts
        assert set(hosts) == {"localhost", "127.0.0.1"}

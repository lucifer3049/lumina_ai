"""驗收：租戶層設定的讀寫（09 §2.6 的 `/settings`，工作包 2C-1）。

`test_tenant_settings_layers.py` 驗的是「三層怎麼疊」，這一檔驗的是**存進哪裡、誰驗
它、以及寫完之後檢索是不是真的看到了新值**——最後那一條是整包的驗收點。

寫入端沿用 2B-5 的宣告（`services/knowledge/kb_config.SECTIONS`）：**上下限只准有一
份**。租戶層另寫一套的話，兩份漂掉時兩邊各自都會綠，而症狀是「同一個參數在 KB 填得
進去、在租戶層填不進去」。

`quota` 是這一層獨有的區塊（KB 沒有配額）。它從 2A 起就住在 `tenant.settings["quota"]`
並由 `QuotaService` 以**容忍**的方式讀取——本包補上寫入端的嚴格檢查，但不改讀取端：
DB 裡已經有的值沒有經過任何驗證，讀取端一旦變嚴格，那些租戶會在下一次檢查額度時被
鎖死。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from apps.identity.models import Tenant
from core.exceptions import NotFoundError, ValidationFailedError
from services.platform.settings import TenantSettingsService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def tenants() -> None:
    for tenant_id, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=slug)


def _stored(tenant_id: uuid.UUID) -> dict[str, Any]:
    with tenant_scope(tenant_id):
        return dict(Tenant.objects.get(id=tenant_id).settings or {})


def _errors(exc: ValidationFailedError) -> list[dict[str, str]]:
    return list(exc.details.get("errors", []))


class TestRead:
    def test_a_fresh_tenant_has_no_overrides(self, tenants: None) -> None:
        assert TenantSettingsService().get(TENANT_A).settings == {}

    def test_it_reads_back_what_was_written(self, tenants: None) -> None:
        """只能寫不能讀的話，設定畫面要嘛自己記一份（會與 DB 漂），要嘛每次顯示空白
        ——而空白與「沒有覆寫」在畫面上長得一樣（同 2B-5 的 KB `config`）。"""
        service = TenantSettingsService()
        service.update(TENANT_A, {"retrieval": {"top_k": 12}})

        assert service.get(TENANT_A).settings == {"retrieval": {"top_k": 12}}


class TestWrite:
    def test_a_partial_update_keeps_the_other_sections(self, tenants: None) -> None:
        """**部分更新是逐區的**：只送 `retrieval` 不該把 `quota` 清掉。

        整份取代的話，設定畫面上「參數」與「配額」是兩個分頁，存其中一頁會清掉另一
        頁——而 API 回 200。
        """
        service = TenantSettingsService()
        service.update(TENANT_A, {"quota": {"tokens_month": 500}})

        service.update(TENANT_A, {"retrieval": {"top_k": 12}})

        assert _stored(TENANT_A) == {"quota": {"tokens_month": 500}, "retrieval": {"top_k": 12}}

    def test_an_empty_section_clears_that_section(self, tenants: None) -> None:
        """`{}` 是「清掉這一區的覆寫」的明確語意——使用者把調壞的設定還原的唯一出路
        （同 2B-5 的 KB `config`）。"""
        service = TenantSettingsService()
        service.update(TENANT_A, {"retrieval": {"top_k": 12}})

        service.update(TENANT_A, {"retrieval": {}})

        assert _stored(TENANT_A).get("retrieval") == {}

    def test_unknown_sections_are_rejected_field_by_field(self, tenants: None) -> None:
        """打錯一個字母（`retreival`）是這一層最主要要擋的東西：合法的 JSON、存得
        進去、看得見，只是永遠不生效。"""
        with pytest.raises(ValidationFailedError) as rejected:
            TenantSettingsService().update(TENANT_A, {"retreival": {"top_k": 12}})

        errors = _errors(rejected.value)
        assert [error["field"] for error in errors] == ["settings.retreival"]
        # 欄位名前綴是 `settings.` 而不是 `config.`——2C-4 的畫面靠它標到對的輸入框，
        # 而同一個畫面上同時有租戶層與 KB 層兩組輸入。
        assert "retrieval" in errors[0]["message"]

    def test_unknown_keys_and_bad_ranges_are_reported_together(self, tenants: None) -> None:
        """一次回報全部的錯：一次只講一個的話，使用者要來回試很多次。"""
        with pytest.raises(ValidationFailedError) as rejected:
            TenantSettingsService().update(
                TENANT_A, {"retrieval": {"top_kk": 1, "top_k": 99999, "rerank_threshold": "高"}}
            )

        fields = {error["field"] for error in _errors(rejected.value)}
        assert fields == {
            "settings.retrieval.top_kk",
            "settings.retrieval.top_k",
            "settings.retrieval.rerank_threshold",
        }

    def test_nothing_is_written_when_validation_fails(self, tenants: None) -> None:
        """驗證在寫任何一區**之前**（同 2B-5 的 KB 更新）：擋在後面的話，一個被拒的
        請求會留下「這一區改了、那一區沒改」的半套狀態，而使用者收到的是 422。"""
        service = TenantSettingsService()
        service.update(TENANT_A, {"retrieval": {"top_k": 12}})

        with pytest.raises(ValidationFailedError):
            service.update(TENANT_A, {"chunk": {"target_tokens": 256}, "retrieval": {"top_k": -1}})

        assert _stored(TENANT_A) == {"retrieval": {"top_k": 12}}

    def test_the_bounds_come_from_the_shared_declaration(self, tenants: None) -> None:
        """上下限只准有一份（2B-5 的 `SECTIONS`）。這裡驗的是「租戶層用的是同一份」
        ——另寫一套的話，同一個參數會在 KB 填得進去、在租戶層填不進去。"""
        from services.knowledge.kb_config import SECTIONS

        spec = SECTIONS["retrieval"]["top_k"]
        assert spec.high is not None
        service = TenantSettingsService()

        service.update(TENANT_A, {"retrieval": {"top_k": spec.high}})
        with pytest.raises(ValidationFailedError):
            service.update(TENANT_A, {"retrieval": {"top_k": spec.high + 1}})


class TestQuotaOverrides:
    """配額覆寫從 2A 起就住在這一欄，本包補上寫入端的檢查。"""

    def test_a_known_resource_is_accepted(self, tenants: None) -> None:
        TenantSettingsService().update(TENANT_A, {"quota": {"tokens_month": 500}})

        assert _stored(TENANT_A)["quota"] == {"tokens_month": 500}

    def test_null_means_unlimited(self, tenants: None) -> None:
        """`None` 是「這個租戶不限制」的明確語意（`resolve_limits`），不是「沒給」。"""
        TenantSettingsService().update(TENANT_A, {"quota": {"tokens_month": None}})

        assert _stored(TENANT_A)["quota"] == {"tokens_month": None}

    def test_an_unknown_resource_is_rejected(self, tenants: None) -> None:
        """`RESOURCES` 之外的鍵會被 `resolve_limits` 安靜忽略——存得進去、永不生效。"""
        with pytest.raises(ValidationFailedError) as rejected:
            TenantSettingsService().update(TENANT_A, {"quota": {"tokens_week": 500}})

        assert _errors(rejected.value)[0]["field"] == "settings.quota.tokens_week"

    def test_a_negative_limit_is_rejected(self, tenants: None) -> None:
        with pytest.raises(ValidationFailedError):
            TenantSettingsService().update(TENANT_A, {"quota": {"documents": -1}})

    def test_the_write_takes_effect_on_the_next_check(self, tenants: None) -> None:
        """改完配額要真的算數——這一條同時證明 `QuotaService` 讀的是同一個地方。"""
        from services.platform.quota import QuotaService

        TenantSettingsService().update(TENANT_A, {"quota": {"documents": 3}})

        assert QuotaService().limits(TENANT_A)["documents"] == 3


class TestItActuallyTakesEffect:
    """**整包的驗收點**：寫進去的值要改變檢索與切塊真正用的參數。"""

    def test_retrieval_params_reflect_the_tenant_layer(self, tenants: None) -> None:
        from services.rag.params import resolve_rag_params

        TenantSettingsService().update(TENANT_A, {"retrieval": {"top_k": 11}})
        tenant_config = TenantSettingsService().param_config(TENANT_A)

        assert resolve_rag_params(None, tenant_config=tenant_config).top_k == 11

    def test_param_config_excludes_non_parameter_sections(self, tenants: None) -> None:
        """`quota` 不是檢索/切塊參數——混進去的話，`read_param` 會在一個它不認識的區
        塊上找鍵，而那只是浪費；真正的風險是日後憑證（2C-2）也住在同一欄，那時
        「整份 settings 直接當參數用」會把密文餵進參數解析。"""
        TenantSettingsService().update(
            TENANT_A, {"quota": {"documents": 3}, "retrieval": {"top_k": 11}}
        )

        assert set(TenantSettingsService().param_config(TENANT_A)) == {"retrieval"}


class TestIsolation:
    def test_one_tenants_settings_are_invisible_to_another(self, tenants: None) -> None:
        service = TenantSettingsService()
        service.update(TENANT_A, {"retrieval": {"top_k": 11}})

        assert service.get(TENANT_B).settings == {}

    def test_a_missing_tenant_is_not_found(self) -> None:
        """租戶不存在時 raise 而不是回空的：安靜地回 `{}` 的話，一個帶著壞 tenant_id
        的呼叫會拿到「這個租戶沒有任何覆寫」，然後照系統預設跑下去。"""
        with pytest.raises(NotFoundError):
            TenantSettingsService().get(uuid.uuid4())

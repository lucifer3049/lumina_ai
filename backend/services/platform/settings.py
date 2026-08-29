"""TenantSettingsService —— 三層覆寫的**中間層**（09 §2.6、15 §4.1，2C-1）。

    系統預設（`app_settings`，env 可蓋）
      → **租戶設定（這一層）**
        → KB 覆寫（`knowledge_base.config`）

這一層從 1D-5 起就寫在 `services/rag/params.py` 的 docstring 上，而它到本包才存在。
**不生效與不存在在畫面上長得一樣**：後台填得進去、讀得回來、看得見，而問答用的還是
系統預設——15 §4.1 整條決定要防的就是那個症狀。

**存在 `tenant.settings` 這一欄**（05 §3.1 的 `settings jb`），與 2A 起就住在那裡的
`quota` 覆寫同一個地方。另開一張表的話，租戶層的東西會有兩個家，而 `QuotaService`
讀的是舊的那個。

**寫入端的驗證共用 2B-5 的宣告**（`services/knowledge/kb_config.SECTIONS`）：上下限
只准有一份，另寫一套的話兩邊各自都會綠，而症狀是「同一個參數在 KB 填得進去、在租戶
層填不進去」。`quota` 是這一層獨有的區塊（KB 沒有配額），它的規則在本檔。

**讀取端不變嚴格**：`QuotaService` 從 2A 起就以容忍的方式讀 `quota`，而 DB 裡已經有
的值沒有經過任何驗證（2C 之前沒有寫入路徑，Django Admin 與 SQL 都寫得到）。讀取端一
旦變嚴格，那些租戶會在下一次檢查額度時被鎖死。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from config.logging import get_logger
from core import audit
from core.exceptions import NotFoundError, ValidationFailedError
from core.request_cache import cached
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import TenantRepository
from services.knowledge.kb_config import SECTIONS, validate_param_sections
from services.platform.credentials import CREDENTIAL_NAMES, CredentialService, CredentialView
from services.platform.quota import RESOURCES

logger = get_logger(__name__)

__all__ = [
    "CREDENTIALS_SECTION",
    "PARAM_SECTIONS",
    "QUOTA_SECTION",
    "TenantSettingsService",
    "TenantSettingsView",
]

# 這一層可寫的區塊：參數兩區（與 KB 共用宣告）＋ 配額一區。
PARAM_SECTIONS = tuple(SECTIONS)
QUOTA_SECTION = "quota"
# 憑證**不存在 `tenant.settings` 裡**（2C-2）：那一欄會整份回給前端，也會被
# `param_config` 讀去解析參數。這個區塊只是 PATCH 的入口，值一律轉手給
# `CredentialService` 加密落地，然後從 settings 裡消失。
CREDENTIALS_SECTION = "credentials"


class TenantSettingsInvalidError(ValidationFailedError):
    """→ 422 + `errors[]`（09 §1.3）。欄位名用 ``settings.<區>.<鍵>``。

    前綴與 KB 的 ``config.`` 不同是刻意的：2C-4 的畫面上同時有租戶層與 KB 層兩組
    輸入，共用前綴的話它標不出錯的是哪一組。
    """

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("租戶設定不合法", details={"errors": errors})


@dataclass(frozen=True)
class TenantSettingsView:
    settings: dict[str, Any]
    # 憑證的**遮罩**（名稱、末四碼、更新時間）——明文永遠不在這裡（09 §2.6）。
    credentials: list[CredentialView] = field(default_factory=list)


class TenantSettingsService:
    def __init__(
        self,
        *,
        tenants: TenantRepository | None = None,
        credentials: CredentialService | None = None,
    ) -> None:
        self._tenants = tenants or TenantRepository()
        self._credentials = credentials or CredentialService()

    def get(self, tenant_id: uuid.UUID) -> TenantSettingsView:
        """設定 + **憑證的遮罩**（名稱、末四碼、更新時間）。明文沒有任何回讀路徑。"""
        with tenant_context(tenant_id), unit_of_work():
            settings = dict(self._require(tenant_id).settings or {})
        return TenantSettingsView(
            settings=settings, credentials=self._credentials.describe(tenant_id)
        )

    def update(self, tenant_id: uuid.UUID, patch: Mapping[str, Any] | None) -> TenantSettingsView:
        """逐區的部分更新：沒送到的區塊原封不動。

        **整份取代的話**，設定畫面上「參數」與「配額」是兩個分頁，存其中一頁會清掉
        另一頁——而 API 回 200。空物件 ``{}`` 則是「清掉這一區的覆寫」的明確語意，
        那是使用者把調壞的設定還原的唯一出路（同 2B-5 的 KB `config`）。

        **驗證在寫任何一區之前**：擋在後面的話，一個被拒的請求會留下「這一區改了、
        那一區沒改」的半套狀態，而使用者收到的是 422。
        """
        validated, credentials = self._validate(patch)
        # **憑證先落地，而且走的是另一張表**（2C-2）：`tenant.settings` 會整份回給
        # 前端、也會被 `param_config` 讀去解析參數，明文進去就是同時對三個地方外流。
        for name, secret in credentials.items():
            if secret is None:
                self._revoke(tenant_id, name)
            else:
                self._credentials.put(tenant_id, name, secret.strip())

        with tenant_context(tenant_id), unit_of_work():
            tenant = self._require(tenant_id)
            before = dict(tenant.settings or {})
            merged = {**before, **validated}
            # 租戶層的參數變更影響**所有人**問到的答案，而症狀（「最近答得怪怪的」）
            # 與這次變更之間隔著幾天——那時唯一查得到「誰、什麼時候、從什麼改成
            # 什麼」的地方就是稽核（2A-4）。before 兩邊都要，只記 after 的話看得到
            # 「現在是多少」，看不到「本來是多少」。
            #
            # **憑證只記名字不記值**：`platform_auditlog` 是刻意不可刪改的（05 §3.5），
            # 金鑰寫進去就收不回來。而「誰在什麼時候換掉了哪一把」正是最該留的那一列，
            # 所以也不能整段不記。
            audit.describe(
                before=before,
                after={**merged, **_credential_audit(credentials)},
            )
            self._tenants.update_settings(tenant_id, merged)
            return TenantSettingsView(
                settings=merged, credentials=self._credentials.describe(tenant_id)
            )

    def param_config(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        """給讀取端用的那一半：**只有參數區**，不含配額（日後也不含憑證，2C-2）。

        整份 `settings` 直接當參數用的話，`read_param` 會在它不認識的區塊上找鍵——
        現在只是浪費，等憑證住進同一欄之後就是把密文餵進參數解析。

        **每個請求只查一次**（同 `QuotaService.limits`，二次架構審計 F-03）：一次
        問答會問到檢索參數好幾回，而設定在請求中途不會變。
        """
        return dict(
            cached(
                f"tenant:params:{tenant_id}",
                lambda: {
                    name: value
                    for name, value in self.get(tenant_id).settings.items()
                    if name in PARAM_SECTIONS
                },
            )
        )

    def _revoke(self, tenant_id: uuid.UUID, name: str) -> None:
        """撤銷一把憑證。**已經不存在時什麼都不做**：PATCH 是宣告式的——使用者說的是
        「這一把不要了」，而不是「刪掉那一列」；重送一次就 404 的話，一個重試會變成
        錯誤，而狀態其實已經是他要的樣子。"""
        try:
            self._credentials.delete(tenant_id, name)
        except NotFoundError:
            logger.info("credential_already_absent", tenant_id=str(tenant_id), name=name)

    # ── 內部 ────────────────────────────────────────────────

    def _validate(self, patch: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        if patch is None:
            return {}, {}
        if not isinstance(patch, Mapping):
            raise TenantSettingsInvalidError([{"field": "settings", "message": "必須是物件"}])

        quota = patch.get(QUOTA_SECTION, _MISSING)
        credentials = patch.get(CREDENTIALS_SECTION, _MISSING)
        rest = {
            name: value
            for name, value in patch.items()
            if name not in (QUOTA_SECTION, CREDENTIALS_SECTION)
        }

        errors: list[dict[str, str]] = []
        cleaned: dict[str, Any] = {}
        try:
            cleaned = validate_param_sections(
                rest,
                prefix="settings",
                error=TenantSettingsInvalidError,
                also_known=(QUOTA_SECTION, CREDENTIALS_SECTION),
            )
        except TenantSettingsInvalidError as invalid:
            errors += list(invalid.details.get("errors", []))

        if quota is not _MISSING:
            quota_errors = _validate_quota(quota)
            if quota_errors:
                errors += quota_errors
            else:
                cleaned[QUOTA_SECTION] = dict(quota)

        if credentials is not _MISSING:
            errors += _validate_credentials(credentials)

        if errors:
            raise TenantSettingsInvalidError(errors)
        return cleaned, ({} if credentials is _MISSING else dict(credentials))

    def _require(self, tenant_id: uuid.UUID) -> Any:
        """租戶不存在就 404。

        **安靜地回 `{}` 更糟**：一個帶著壞 tenant_id 的呼叫會拿到「這個租戶沒有任何
        覆寫」，然後照系統預設跑完整條路——沒有錯誤，只有一份不屬於任何人的設定。
        """
        tenant = self._tenants.current()
        if tenant is None or str(tenant.id) != str(tenant_id):
            raise NotFoundError("租戶不存在")
        return tenant


class _Missing:
    """「這次沒送」與「送了 null」要分得開——後者在 quota 是「不限制」。"""


_MISSING = _Missing()


def _validate_quota(raw: Any) -> list[dict[str, str]]:
    """配額覆寫：鍵必須是 `RESOURCES` 之一，值是非負整數或 `None`。

    `resolve_limits` 對不認識的鍵與壞值一律**安靜忽略**（讀取端容忍），所以少了這道
    檢查，`{"tokens_week": 500}` 會存得進去、在畫面上看得見，然後永遠不生效。
    """
    if not isinstance(raw, Mapping):
        return [{"field": f"settings.{QUOTA_SECTION}", "message": "必須是物件"}]

    errors: list[dict[str, str]] = []
    for key, value in raw.items():
        where = f"settings.{QUOTA_SECTION}.{key}"
        if str(key) not in RESOURCES:
            errors.append(
                {"field": where, "message": f"不是可設定的配額；可用的有 {'、'.join(RESOURCES)}"}
            )
            continue
        if value is None:
            # 明確的「這個租戶不限制」（`resolve_limits`）。
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append({"field": where, "message": f"必須是整數或 null（收到 {value!r}）"})
        elif value < 0:
            errors.append({"field": where, "message": f"不得為負數（收到 {value!r}）"})
    return errors


def _validate_credentials(raw: Any) -> list[dict[str, str]]:
    """憑證：鍵是白名單裡的名字，值是非空字串（設定）或 `None`（撤銷）。

    **名字打錯會存進去、在畫面上看得見，而沒有任何東西會去讀它**——與 2B-5 擋
    `retreival` 是同一種錯誤，只是這一次錯的東西是「provider 拿不到金鑰」。

    空字串同樣要擋：存得進去、畫面顯示「已設定」，而每一次呼叫 provider 都是 401。
    """
    if not isinstance(raw, Mapping):
        return [{"field": f"settings.{CREDENTIALS_SECTION}", "message": "必須是物件"}]

    errors: list[dict[str, str]] = []
    for key, value in raw.items():
        where = f"settings.{CREDENTIALS_SECTION}.{key}"
        if str(key) not in CREDENTIAL_NAMES:
            errors.append(
                {
                    "field": where,
                    "message": f"不是可設定的憑證；可用的有 {'、'.join(CREDENTIAL_NAMES)}",
                }
            )
            continue
        if value is None:
            # 明確的「撤銷這一把」，與「這次沒送」分得開。
            continue
        # **訊息不得帶上收到的值**（鐵則 9）：它就是那把金鑰。
        if not isinstance(value, str) or not value.strip():
            errors.append({"field": where, "message": "必須是非空字串，或 null（撤銷）"})
    return errors


def _credential_audit(credentials: Mapping[str, Any]) -> dict[str, Any]:
    """稽核上的憑證變更：**只有名字與動作**。

    值一律不進去（鐵則 9、10 §5）。整段不記也不行——「誰換掉了 provider 金鑰」是這張
    表最該有的一列之一。
    """
    if not credentials:
        return {}
    return {
        CREDENTIALS_SECTION: {
            name: ("revoked" if value is None else "updated") for name, value in credentials.items()
        }
    }

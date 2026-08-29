"""CredentialService —— provider 憑證的加密落地（10 §5、09 §2.6，2C-2）。

envelope 的三層在這裡合起來：

    KEK（env 或 `.secrets/`，`core/crypto.py`）
      → per-tenant DEK（`platform_tenantdatakey`，以 KEK 包起來存）
        → 憑證密文（`platform_credential`，以 DEK 加密）

**中間那一層的意義在輪替 KEK 的那一天**：只要重包 N 把 DEK，不必把每一筆憑證解開再
加密一遍。租戶數遠少於憑證數，而這個差距只會愈拉愈大。

**唯寫不回讀**（09 §2.6 的那一列）：對外只有「設過沒有、末四碼、什麼時候換的」。
`get_secret` 是給**要拿去用的那一刻**呼叫的，回傳值不得記進 log、不得進回應、不得
進稽核（10 §5：「解密僅在使用當下、不落 log」）。

**名字是白名單**：打錯的話它會存進去、在畫面上看得見，而沒有任何東西會去讀它——與
2B-5 擋 `retreival` 是同一種錯誤，只是這一次錯的東西是「provider 拿不到金鑰」。
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from config.logging import get_logger
from core.crypto import generate_key, get_kek, open_sealed, seal
from core.exceptions import NotFoundError
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.platform import CredentialRepository, TenantDataKeyRepository

logger = get_logger(__name__)

__all__ = ["CREDENTIAL_NAMES", "CredentialService", "CredentialView"]

# 可設定的憑證。**白名單而不是自由字串**（見模組 docstring）。
#
# 目前只有 provider 金鑰；同步來源的憑證（2D 的 Website/DB loader）進來時加在這裡，
# 而不是讓呼叫端各自約定一個字串。
CREDENTIAL_NAMES: tuple[str, ...] = (
    "openai_api_key",
    "gemini_api_key",
    "jina_api_key",
)

# 末四碼。**不存前四碼**：`sk-live-` 這類前綴洩漏的是種類與環境。
_HINT_CHARS = 4


@dataclass(frozen=True)
class CredentialView:
    """對外的樣子——**沒有明文，也沒有密文**。"""

    name: str
    hint: str
    updated_at: datetime


class CredentialService:
    def __init__(
        self,
        *,
        credentials: CredentialRepository | None = None,
        data_keys: TenantDataKeyRepository | None = None,
    ) -> None:
        self._credentials = credentials or CredentialRepository()
        self._data_keys = data_keys or TenantDataKeyRepository()

    def put(self, tenant_id: uuid.UUID, name: str, secret: str) -> CredentialView:
        """寫入（同名覆寫）。

        覆寫而不是新增一列：換金鑰是常見操作，而兩列之後 `get_secret` 得決定「哪一列
        才算數」——挑錯的症狀是「換了 key 還是 401」。
        """
        with tenant_context(tenant_id), unit_of_work():
            dek = self._dek(tenant_id)
            row = self._credentials.upsert(
                name=name,
                ciphertext=seal(dek, secret),
                hint=secret[-_HINT_CHARS:],
            )
            # **只記名字**：這一行的存在本身就是「誰換了哪一把」的線索，而內容不進
            # log（10 §5）。
            logger.info("credential_stored", tenant_id=str(tenant_id), name=name)
            return _view(row)

    def get_secret(self, tenant_id: uuid.UUID, name: str) -> str:
        """解密——**只在要用的那一刻呼叫**，回傳值不得落進 log／回應／稽核。

        找不到時 raise 而不是回 `None`：`None` 會被呼叫端當成 key 送給 provider，而
        錯誤是 provider 回的 401——與「金鑰過期」長得一模一樣，查起來差很多。
        """
        with tenant_context(tenant_id), unit_of_work():
            row = self._credentials.get(name)
            if row is None:
                raise NotFoundError("找不到這個憑證")
            return open_sealed(self._dek(tenant_id), bytes(row.ciphertext))

    def describe(self, tenant_id: uuid.UUID) -> list[CredentialView]:
        """設定畫面看得到的一切：**設過沒有、哪一把、什麼時候換的**。"""
        with tenant_context(tenant_id), unit_of_work():
            return [_view(row) for row in self._credentials.list_all()]

    def delete(self, tenant_id: uuid.UUID, name: str) -> None:
        """撤銷。**硬刪**：留著密文的唯一用途是還原一把使用者認為已經作廢的金鑰。"""
        with tenant_context(tenant_id), unit_of_work():
            if not self._credentials.delete(name):
                raise NotFoundError("找不到這個憑證")
            logger.info("credential_deleted", tenant_id=str(tenant_id), name=name)

    # ── 內部 ────────────────────────────────────────────────

    def _dek(self, tenant_id: uuid.UUID) -> bytes:
        """這個租戶的資料金鑰；第一次用到時建立。

        **建立必須是冪等的**：兩個併發請求各產生一把的話，後寫入的那把會蓋掉先前
        的——而先前那把加密過的憑證從此解不開，且沒有任何錯誤訊息（它只是「解不
        開」，發生在下一次呼叫 provider 時）。靠 `(tenant)` 的 PK 擋，不是靠先查再建。
        """
        row = self._data_keys.get()
        if row is not None:
            return _unwrap(bytes(row.wrapped_key))

        created = self._data_keys.create_if_absent(wrapped_key=_wrap(generate_key()))
        # 讀回**實際落地的那一列**而不是用剛產生的那把：併發時另一個請求可能先寫入，
        # 而 `create_if_absent` 回的是贏家。用手上這把的話，這個請求加密出來的東西
        # 之後沒有人解得開。
        return _unwrap(bytes(created.wrapped_key))


def _wrap(dek: bytes) -> bytes:
    """DEK → 以 KEK 封裝的位元組。

    中間過一手 base64 是因為 `seal` 收的是字串（憑證本來就是文字），而 DEK 是位元組
    ——base64 是兩者之間唯一無損的那一層。
    """
    return seal(get_kek(), base64.b64encode(dek).decode("ascii"))


def _unwrap(wrapped: bytes) -> bytes:
    return base64.b64decode(open_sealed(get_kek(), wrapped))


def _view(row: Any) -> CredentialView:
    """model → 對外型別。**明確列欄位**（同 `KnowledgeBaseView` 的理由）：把 model
    丟給上層序列化的話，任何人在它上面加一個欄位——下一個就是 `ciphertext`——都會
    自動流到 client，而不會有任何測試紅燈。"""
    return CredentialView(
        name=str(row.name),
        hint=str(row.hint),
        updated_at=row.updated_at,
    )

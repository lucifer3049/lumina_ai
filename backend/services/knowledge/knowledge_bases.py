"""KnowledgeBaseService —— KB 的 CRUD（09 §2.3、04 §Knowledge）。

**交易邊界在這一層**（11 §4.3）：每個公開方法把自己的資料存取包進
:func:`core.uow.unit_of_work`。唯讀查詢也要包——RLS policy 對 ``SELECT`` 同樣生效，
而 policy 讀的 ``app.tenant_id`` 是交易區域參數；沒有交易就沒有那個值，查詢會回空
集合而不是報錯（05 §5.1）。

方法全部是**同步**的：由 `core.db.run_orm` 從 threadpool 呼叫（ADR-001）。

**回傳 dataclass 而不是 model**：`services/` 依鐵則 2 看不到 `apps.*` 的型別，而且
把 model 丟給上層序列化的話，任何人在 model 上加一個內部欄位（下一個就是
``storage_key``）都會自動流到 client，不會有任何測試紅燈。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core import audit
from core.exceptions import NotFoundError
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.knowledge import DocumentRepository, KnowledgeBaseRepository
from services.knowledge.kb_config import validate_kb_config


@dataclass(frozen=True)
class KnowledgeBaseView:
    id: uuid.UUID
    name: str
    description: str
    status: str
    # 前端顯示「N 份文件」用。放進詳情與列表是為了免掉「列完 KB 再逐一打文件端點」
    # 的 N+1 往返——那在 KB 數量多時是使用者可感知的延遲。
    document_count: int
    # KB 級的參數覆寫（05 §3.2、15 §4.1 的第三層，2B-5）。**只能寫不能讀的話**，
    # 設定畫面要嘛自己記一份（會與 DB 漂），要嘛每次都顯示空白——而空白與「沒有
    # 覆寫」在畫面上長得一樣。
    config: dict[str, Any]


class KnowledgeBaseService:
    def __init__(
        self,
        *,
        knowledge_bases: KnowledgeBaseRepository | None = None,
        documents: DocumentRepository | None = None,
    ) -> None:
        self._knowledge_bases = knowledge_bases or KnowledgeBaseRepository()
        self._documents = documents or DocumentRepository()

    def list_knowledge_bases(self, tenant_id: uuid.UUID) -> list[KnowledgeBaseView]:
        with tenant_context(tenant_id), unit_of_work():
            return [self._view(kb) for kb in self._knowledge_bases.list_all()]

    def get(self, tenant_id: uuid.UUID, kb_id: uuid.UUID) -> KnowledgeBaseView:
        with tenant_context(tenant_id), unit_of_work():
            return self._view(self._require(kb_id))

    def create(
        self,
        tenant_id: uuid.UUID,
        *,
        name: str,
        description: str = "",
        config: Mapping[str, Any] | None = None,
    ) -> KnowledgeBaseView:
        """建立。`config` 走**與更新同一條驗證**（`validate_kb_config`）。

        兩條各驗一次的話，其中一條遲早會漏掉新加的參數，而使用者會發現「建立時填得
        進去、修改時不行」——或反過來，那更糟：他建了一個帶著非法設定的 KB。
        """
        validated = validate_kb_config(config)
        with tenant_context(tenant_id), unit_of_work():
            kb = self._knowledge_bases.create(name=name, description=description, config=validated)
            # 建立類的 id 不在 URL 上，稽核 middleware 看不到（2A-4）。
            audit.describe(resource_id=kb.id)
            return self._view(kb)

    def update(
        self,
        tenant_id: uuid.UUID,
        kb_id: uuid.UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> KnowledgeBaseView:
        """部分更新：``None`` 代表「這次沒給」，不是「設為空」。

        兩者混淆的症狀是使用者改一次名稱、描述就被清空——而 API 回 200，看起來成功。
        `config` 的「設為空」是明確的 ``{}``（清掉全部覆寫、回到系統預設），那是使用者
        把一個調壞的 KB 還原的唯一出路。

        **驗證在寫任何一個欄位之前**（09 §1.3）：擋在後面的話，一個被拒的 config 會
        留下「name 已經改掉、config 沒改」的半套狀態，而使用者收到的是 422——他不會
        知道另一半已經生效了。
        """
        validated = None if config is None else validate_kb_config(config)
        changes: dict[str, Any] = {
            field: value
            for field, value in (("name", name), ("description", description))
            if value is not None
        }

        with tenant_context(tenant_id), unit_of_work():
            before = self._require(kb_id)
            if validated is not None:
                changes["config"] = validated
                # 05 §81：`knowledge_version` 是「設定變更遞增」，用途是「這個 KB
                # 需要重建嗎」（2B-6 的 reindex 靠它判定）。**只有切塊那一區算數**：
                # 既有的 chunk 是用舊參數切出來的，而檢索參數是讀路徑的旋鈕，改它
                # 不影響任何已經存在的 chunk——跟著遞增的話，每一次微調 top_k 都會
                # 讓那個 KB 看起來「需要重建」，而重建一次是整庫重新嵌入的錢。
                if _chunking_changed(dict(before.config or {}), validated):
                    changes["knowledge_version"] = int(before.knowledge_version) + 1
                # 改設定會改變**所有人**問到的答案，而症狀（「最近答得怪怪的」）與這
                # 次變更之間隔著幾天——那時唯一查得到「誰、什麼時候、從什麼改成什麼」
                # 的地方就是稽核（2A-4 的 `knowledge_base.update`）。
                audit.describe(
                    before={"config": dict(before.config or {})}, after={"config": validated}
                )
            if changes:
                self._knowledge_bases.update(kb_id, **changes)
            return self._view(self._require(kb_id))

    def delete(self, tenant_id: uuid.UUID, kb_id: uuid.UUID) -> None:
        """軟刪除（05 §5.4）。文件與 chunk 的硬刪由清理 worker 分批處理。

        **文件不在這裡連帶標記**：KB 已經查不到，而文件的可見性也走 KB 這條路
        （列表端點先要 KB 存在，檢索則先經 `RetrievalService._find_kb`）。逐一標記
        數萬份文件會讓刪除變成長交易，那正是 05 §5.4 要避免的。真正的級聯清理由
        `DeletedKnowledgePurgeService` 在保留窗過後分批做——它認得「KB 已刪但文件
        自己沒有 ``deleted_at``」這種形狀，那是這個取捨的必要配套。
        """
        with tenant_context(tenant_id), unit_of_work():
            kb = self._require(kb_id)
            # 刪除是唯一「事後查不到現場」的操作——稽核列上的 before 是這個 KB
            # 存在過的唯一證據（04 §8.3 明列 KB 刪除，2A-4）。
            audit.describe(before={"name": kb.name})
            self._knowledge_bases.soft_delete(kb_id)

    def _require(self, kb_id: uuid.UUID) -> Any:
        """取 KB，不存在就 404。

        **不存在與屬於別的租戶回同一個錯誤**是刻意的（09 §2.3 資源類規則）：回 403
        等於承認「這個 id 存在，只是你不能碰」，那讓人可以拿 id 逐一嘗試，掃出別的
        租戶有哪些 KB。Repository 的 tenant filter 讓兩種情況在這裡本來就分不出來。
        """
        kb = self._knowledge_bases.get_by_id(kb_id)
        if kb is None:
            raise NotFoundError("知識庫不存在")
        return kb

    def _view(self, kb: Any) -> KnowledgeBaseView:
        return KnowledgeBaseView(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            status=kb.status,
            document_count=self._documents.count_for_kb(kb.id),
            config=dict(kb.config or {}),
        )


def _chunking_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """切塊那一區的**值**變了沒有。

    比值而不是「這次有沒有送 chunk 區」：設定畫面每次儲存都會把整份 config 送回來
    （`config` 是整份取代），所以「有送就遞增」等於「每按一次儲存就要求重建一次整個
    知識庫」——而使用者什麼都沒改。
    """
    return bool(before.get("chunk", {}) != after.get("chunk", {}))

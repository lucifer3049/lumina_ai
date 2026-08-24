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
from dataclasses import dataclass
from typing import Any

from core import audit
from core.exceptions import NotFoundError
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.knowledge import DocumentRepository, KnowledgeBaseRepository


@dataclass(frozen=True)
class KnowledgeBaseView:
    id: uuid.UUID
    name: str
    description: str
    status: str
    # 前端顯示「N 份文件」用。放進詳情與列表是為了免掉「列完 KB 再逐一打文件端點」
    # 的 N+1 往返——那在 KB 數量多時是使用者可感知的延遲。
    document_count: int


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
        self, tenant_id: uuid.UUID, *, name: str, description: str = ""
    ) -> KnowledgeBaseView:
        with tenant_context(tenant_id), unit_of_work():
            kb = self._knowledge_bases.create(name=name, description=description)
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
    ) -> KnowledgeBaseView:
        """部分更新：``None`` 代表「這次沒給」，不是「設為空」。

        兩者混淆的症狀是使用者改一次名稱、描述就被清空——而 API 回 200，看起來成功。
        """
        changes = {
            field: value
            for field, value in (("name", name), ("description", description))
            if value is not None
        }

        with tenant_context(tenant_id), unit_of_work():
            self._require(kb_id)
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
        )

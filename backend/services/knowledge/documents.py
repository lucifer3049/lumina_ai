"""DocumentService —— 文件的讀取與刪除（09 §2.3）。

上傳（`POST /knowledge-bases/{id}/documents`）屬 1B-3，不在本模組；ETL 的重跑
（reingest）屬 1B-6。本階段的文件只可能由測試 factory 產生。

交易邊界與回傳型別的規則同 `knowledge_bases.py`，不重述。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from core.exceptions import NotFoundError
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.knowledge import DocumentRepository, KnowledgeBaseRepository


@dataclass(frozen=True)
class DocumentView:
    """對外的文件表示。

    **刻意沒有 ``storage_key``。** 它是物件儲存的內部路徑
    （``tenant-{slug}/kb/{kb_id}/{doc_id}``），同時洩漏儲存結構與租戶 slug；1B-3
    之後那是一個可以直接拿去嘗試存取的字串。使用者要知道的是「這份文件處理到哪了」，
    不是「它存在哪」。

    ``error`` 是結構化的失敗原因（08 §6 的 DLQ 內容），ETL 尚未接上前恆為 ``None``。
    """

    id: uuid.UUID
    kb_id: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    status: str
    doc_version: int
    error: dict[str, Any] | None


class DocumentService:
    def __init__(
        self,
        *,
        documents: DocumentRepository | None = None,
        knowledge_bases: KnowledgeBaseRepository | None = None,
    ) -> None:
        self._documents = documents or DocumentRepository()
        self._knowledge_bases = knowledge_bases or KnowledgeBaseRepository()

    def list_for_kb(self, tenant_id: uuid.UUID, kb_id: uuid.UUID) -> list[DocumentView]:
        with tenant_context(tenant_id), unit_of_work():
            # 先確認 KB 存在：直接查文件的話，不存在的 KB 會回空陣列而不是 404，
            # 而「空的知識庫」與「打錯 id」對使用者是完全不同的兩件事。
            if self._knowledge_bases.get_by_id(kb_id) is None:
                raise NotFoundError("知識庫不存在")
            return [self._view(document) for document in self._documents.for_kb(kb_id)]

    def get(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> DocumentView:
        with tenant_context(tenant_id), unit_of_work():
            return self._view(self._require(document_id))

    def delete(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> None:
        """軟刪除（05 §5.4）。chunk 與 embedding 的硬刪由清理 worker 負責。"""
        with tenant_context(tenant_id), unit_of_work():
            self._require(document_id)
            self._documents.soft_delete(document_id)

    def _require(self, document_id: uuid.UUID) -> Any:
        """不存在與跨租戶回同一個 404（理由見 `KnowledgeBaseService._require`）。"""
        document = self._documents.get_by_id(document_id)
        if document is None:
            raise NotFoundError("文件不存在")
        return document

    def _view(self, document: Any) -> DocumentView:
        return DocumentView(
            id=document.id,
            kb_id=document.kb_id,
            filename=document.filename,
            mime_type=document.mime_type,
            size_bytes=document.size_bytes,
            status=document.status,
            doc_version=document.doc_version,
            error=document.error,
        )

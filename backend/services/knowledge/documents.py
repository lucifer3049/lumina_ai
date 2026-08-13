"""DocumentService —— 文件的上傳、讀取與刪除（09 §2.3、§3.1）。

大檔（>32MB）的 presigned 分塊直傳與 ETL 重跑（reingest）屬後續工作包。

交易邊界與回傳型別的規則同 `knowledge_bases.py`，不重述。

**上傳是這個系統第一個同時寫 DB 與物件儲存的路徑**，兩者無法放進同一個交易，
所以順序與失敗處理要明講（見 :meth:`DocumentService.upload`）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from django.db import IntegrityError

from config.logging import get_logger
from core.exceptions import ConflictError, NotFoundError
from core.object_storage import build_document_key, delete_object, put_object
from core.tasks import enqueue_ingestion
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.knowledge import DocumentRepository, KnowledgeBaseRepository
from services.knowledge.uploads import detect_media_type, ensure_within_limit, sha256_of

logger = get_logger(__name__)


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

    def upload(
        self, tenant_id: uuid.UUID, kb_id: uuid.UUID, *, filename: str, content: bytes
    ) -> DocumentView:
        """單請求上傳（09 §3.1 的小檔路徑）。

        **順序是刻意的**，四步各自對應一種失敗：

        1. **先驗內容**（大小、magic bytes）——在碰物件儲存**之前**。反過來寫比較
           方便（拿得到完整檔案再判斷），但那等於任何人都能把任意內容寫進我們的
           bucket，白名單形同虛設。
        2. **再查 KB 與重複**——都是便宜的 DB 查詢，能在上傳之前擋掉的就不要先寫。
        3. **PUT 物件**。
        4. **寫 DB；失敗就把物件刪回去。**

        步驟 3、4 的順序是「先物件再 DB」，代價是 DB 失敗時可能留下孤兒物件，因此
        有第 4 步的回收。反過來（先寫 DB 再 PUT）的代價是「卡在 uploading 狀態的
        列」，而那會出現在使用者的文件列表上——**看得到的壞狀態比看不到的垃圾更糟**。

        回收是 best-effort：刪不掉就記 log 讓清理流程處理，不能因為回收失敗而把
        原本的錯誤蓋掉（使用者要看到的是「上傳失敗」，不是「刪除暫存檔失敗」）。
        """
        ensure_within_limit(len(content))
        media_type = detect_media_type(content, filename=filename)
        content_hash = sha256_of(content)
        document_id = uuid.uuid4()

        with tenant_context(tenant_id), unit_of_work():
            if self._knowledge_bases.get_by_id(kb_id) is None:
                raise NotFoundError("知識庫不存在")
            if self._documents.find_by_content_hash(kb_id=kb_id, content_hash=content_hash):
                raise ConflictError("這份文件已經在這個知識庫裡")
            storage_key = build_document_key(kb_id=kb_id, document_id=document_id)

        put_object(storage_key, content, content_type=media_type)

        try:
            with tenant_context(tenant_id), unit_of_work():
                document = self._documents.create(
                    document_id=document_id,
                    kb_id=kb_id,
                    filename=filename,
                    mime_type=media_type,
                    storage_key=storage_key,
                    content_hash=content_hash,
                    size_bytes=len(content),
                )
        except Exception as exc:
            self._discard(storage_key)
            if isinstance(exc, IntegrityError):
                # 上面的重複檢查與這裡之間有競態：兩個併發請求送同一份內容時，
                # 兩邊都會通過檢查，而唯一約束只會讓其中一個成功。對使用者來說
                # 結果與循序上傳完全相同（409），所以不必特別處理，只要別讓
                # IntegrityError 冒成 500。
                raise ConflictError("這份文件已經在這個知識庫裡") from exc
            raise

        # **交易提交之後**才排 ETL（08 §2 的觸發點）。交易內送的話，worker 可能在
        # COMMIT 之前就開始處理而查不到這份文件——症狀是隨機的「文件不存在」，重跑又好。
        enqueue_ingestion(tenant_id=tenant_id, document_id=document.id)

        return self._view(document)

    def _discard(self, storage_key: str) -> None:
        """回收剛上傳的物件；失敗只記 log。

        不讓回收的錯誤覆蓋原本的錯誤：使用者要看到的是「上傳失敗」，不是「刪除
        暫存檔失敗」。留下的孤兒物件不影響正確性（沒有任何 DB 列指向它），由清理
        流程處理。
        """
        try:
            delete_object(storage_key)
        except Exception:
            logger.warning("orphan_object_left_behind", storage_key=storage_key, exc_info=True)

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

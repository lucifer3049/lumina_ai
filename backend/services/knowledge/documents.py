"""DocumentService —— 文件的上傳、讀取、重跑與刪除（09 §2.3、§3.1）。

大檔（>32MB）的 presigned 分塊直傳屬後續工作包。

交易邊界與回傳型別的規則同 `knowledge_bases.py`，不重述。

**上傳是這個系統第一個同時寫 DB 與物件儲存的路徑**，兩者無法放進同一個交易，
所以順序與失敗處理要明講（見 :meth:`DocumentService.upload`）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.db import IntegrityError
from django.utils import timezone

from config.logging import get_logger
from core.exceptions import ConflictError, NotFoundError
from core.object_storage import build_document_key, delete_object, put_object
from core.tasks import enqueue_embedding, enqueue_ingestion
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.knowledge import (
    ChunkRepository,
    DocumentRepository,
    KnowledgeBaseRepository,
)
from services.knowledge.uploads import detect_media_type, ensure_within_limit, sha256_of
from services.platform.quota import QuotaService

logger = get_logger(__name__)

# ETL 進行中的狀態（08 §2）。這幾個狀態下重跑會讓兩個 job 寫同一份文件。
#
# ``cleaned`` 也算進行中：它是「解析完、正在切塊」，下一步就會寫 chunk。漏掉它的話，
# 使用者在那幾秒內按重跑會讓兩個 job 同時寫同一份文件的 chunk，而先寫完的那個會被
# 另一個的「先刪同版本殘留」清掉——結果是隨機少一半內容，兩邊都不報錯。
#
# ``chunked`` **不算**：那時 chunk 已經寫完，重跑是安全的，而它同時是 embedding 沒有
# 被觸發時唯一的恢復入口（見 `EmbeddingService` 對 doc_version 的守門）。
_IN_PROGRESS_STATUSES = {"parsing", "cleaned", "embedding"}

# 停住時可以靠重排恢復的兩個狀態——它們的共通點是「該有一則訊息，而它不在」。
_STATUS_UPLOADED = "uploaded"
_STATUS_CHUNKED = "chunked"


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
        chunks: ChunkRepository | None = None,
        quota: QuotaService | None = None,
    ) -> None:
        self._documents = documents or DocumentRepository()
        self._knowledge_bases = knowledge_bases or KnowledgeBaseRepository()
        self._chunks = chunks or ChunkRepository()
        self._quota = quota or QuotaService()

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
        # 配額擋線（2A-2a），在碰物件儲存之前：文件數與儲存量都是 DB 聚合的存量
        # 檢查（services/platform/quota.py），被擋的請求什麼都不會留下。
        # 32MB 的 ensure_within_limit 與這裡是兩件事：那是單請求的解析資源保護
        # （413、永遠適用），這是租戶的商務額度（429、可談可調）。
        self._quota.check_and_reserve(tenant_id, "documents", 1)
        self._quota.check_and_reserve(tenant_id, "storage_bytes", len(content))
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

    def reingest(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> DocumentView:
        """重新處理（09 §2.3 的 ``POST /documents/{id}/reingest``，回 202）。

        三件事在同一個交易裡（08 §2 的 ``ready → parsing``、``doc_version+1``）：

        1. **舊 chunk 標 superseded**——不是刪除。新版本的 embedding 還沒好，這段期間
           檢索仍要服務得了查詢；刪掉的話重跑的那幾分鐘這份文件會完全查不到。
        2. **doc_version +1**——它是冪等鍵的一部分（08 §6）。不遞增的話，新一輪會查到
           上一版**已成功**的 job 而跳過每個階段，文件停在舊內容、狀態卻是完成。
        3. **狀態回 ``uploaded`` 並清掉 error**——重跑的起點與第一次上傳完全相同。

        處理中的文件不得重跑：那會讓兩個 job 寫同一份文件的 chunk，而先寫完的那個
        會被另一個的「先刪同版本殘留」清掉——結果是隨機少一半內容。
        """
        with tenant_context(tenant_id), unit_of_work():
            document = self._require(document_id)
            if document.status in _IN_PROGRESS_STATUSES:
                raise ConflictError("文件正在處理中，請等它結束再重跑")
            next_version = int(document.doc_version) + 1
            self._chunks.supersede_for_document(document_id)
            self._documents.start_new_version(document_id, doc_version=next_version)
            refreshed = self._require(document_id)
            view = self._view(refreshed)

        enqueue_ingestion(tenant_id=tenant_id, document_id=document_id)
        return view

    def requeue_stuck(
        self, tenant_id: uuid.UUID, *, stale_after_minutes: int, dry_run: bool = False
    ) -> list[tuple[uuid.UUID, str]]:
        """把停住的文件重新排進佇列；回傳 (document_id, status) 清單。

        **這是 enqueue 失敗唯一的恢復路徑。** 送任務是 best-effort（broker 掛掉時
        `enqueue_ingestion` / `enqueue_embedding` 記 log 後回 None，不讓使用者的上傳
        失敗），代價是那份文件停在 ``uploaded`` 或 ``chunked`` 而**沒有任何訊息存在**
        ——不會有人重試，因為沒有東西可重試。

        兩個狀態對應兩條佇列：``uploaded`` 要重跑 ETL，``chunked`` 只差 embedding。
        全部當成 ETL 重排的話，已經切好塊的文件會重新解析一次整份 PDF——結果正確，
        但那是幾分鐘的 CPU 換一件本來只要幾秒的事。

        **不含 ``failed``**：那是「試過而且失敗了」，重排只會再失敗一次；它的入口是
        re-ingest（使用者的明確決定）。也不含 ``parsing`` / ``cleaned`` / ``embedding``
        ——那些狀態下訊息還在飛，`acks_late` 會處理 worker 中途死掉的情況，這裡再排
        一次只是讓兩個 worker 搶同一份文件。

        全租戶掃描需要 BYPASSRLS 角色，而那要等 2A（13 §3.1 v1.7）。因此這支是
        **逐租戶**的：呼叫端要嘛知道自己在修哪個租戶，要嘛在外面迴圈。
        """
        cutoff = timezone.now() - timedelta(minutes=stale_after_minutes)
        with tenant_context(tenant_id), unit_of_work():
            stuck = self._documents.stuck_in(
                [_STATUS_UPLOADED, _STATUS_CHUNKED], not_updated_since=cutoff
            )
            found = [(uuid.UUID(str(doc.id)), str(doc.status)) for doc in stuck]

        if dry_run:
            return found

        for document_id, status in found:
            if status == _STATUS_UPLOADED:
                enqueue_ingestion(tenant_id=tenant_id, document_id=document_id)
            else:
                enqueue_embedding(tenant_id=tenant_id, document_id=document_id)
            logger.info(
                "document_requeued",
                document_id=str(document_id),
                document_status=status,
            )
        return found

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

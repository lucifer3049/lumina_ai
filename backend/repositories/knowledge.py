"""Knowledge 的 Repository —— 租戶隔離的第一道防線（鐵則 4）。

第二道是 RLS policy（apps/knowledge/migrations/0002_rls.py）。兩道的條件必須一致；
不一致時的症狀分兩種，而且都沒有錯誤訊息（程式比 policy 寬 → 回空集合；程式比
policy 窄 → 使用者看不到本來該看到的資料）。

本檔的方法全部是**同步**的：只會被 :func:`core.db.run_orm` 從 threadpool 呼叫，
不得在 async 路徑直接 await（ADR-001）。

**三個查詢語意在這裡定案，錯了都不會報錯**：

1. :meth:`ChunkRepository.for_retrieval` 預設排除 ``superseded``。混進舊版本的話，
   LLM 會拿早已被取代的內容當依據，而引用指向的 chunk 確實存在——回應看起來完全
   正常，事後沒有任何自動化手段能發現。
2. :meth:`EtlJobRepository.find` 以 ``(document, doc_version, stage)`` 定位（08 §6）。
   少了 ``doc_version``，re-ingest 會查到上一版已成功的 job 而跳過該階段，文件停在
   舊內容、狀態卻是 ready。
3. **軟刪除的實體預設不可見**（1B-2）：`KnowledgeBase` 與 `Document` 的
   ``get_queryset`` 排除 ``deleted_at IS NOT NULL``。05 §5.4 的軟刪除是為了「使用者
   可能後悔」（30 天後由清理 job 硬刪），不是為了讓資料繼續出現——只寫 ``deleted_at``
   卻沒從查詢排除的話，使用者會看到自己剛刪掉的東西還在列表上，而刪除 API 回了 204。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from django.db.models import Model, QuerySet
from django.utils import timezone

from apps.knowledge.models import Chunk, Document, EtlJob, KnowledgeBase
from core.tenant import get_current_tenant_id
from repositories.base import TenantScopedRepository


class SoftDeletableRepository[M: Model](TenantScopedRepository[M]):
    """軟刪除的實體：預設查詢排除已刪除的列。

    覆寫 ``get_queryset`` 而不是要求每個查詢自己加條件——後者只要有一處漏掉，
    使用者就會看到已刪除的資料，而那一處不會有任何症狀。要撈已刪除的列（清理
    worker、還原功能）走 :meth:`including_deleted`，讓那個意圖在呼叫端顯式可見。
    """

    def get_queryset(self) -> QuerySet[M]:
        return super().get_queryset().filter(deleted_at__isnull=True)

    def including_deleted(self) -> QuerySet[M]:
        """含已刪除的列——只給清理 worker 與還原流程用。"""
        return super().get_queryset()

    def soft_delete(self, entity_id: uuid.UUID) -> int:
        """標記刪除；回傳影響的列數（0 = 不存在或已刪除）。

        不硬刪（05 §5.4）：硬刪要級聯 embeddings → chunks → documents，那是清理
        worker 分批做的事，在請求路徑上做會鎖表。
        """
        return self.get_queryset().filter(id=entity_id).update(deleted_at=timezone.now())


class KnowledgeBaseRepository(SoftDeletableRepository[KnowledgeBase]):
    model = KnowledgeBase

    def get_by_id(self, kb_id: uuid.UUID) -> KnowledgeBase | None:
        return self.get_queryset().filter(id=kb_id).first()

    def list_all(self) -> list[KnowledgeBase]:
        return list(self.get_queryset().order_by("-created_at"))

    def create(
        self, *, name: str, description: str = "", config: dict[str, object] | None = None
    ) -> KnowledgeBase:
        return KnowledgeBase.objects.create(
            tenant_id=get_current_tenant_id(operation="KnowledgeBaseRepository.create"),
            name=name,
            description=description,
            config=config or {},
        )

    def update(self, kb_id: uuid.UUID, **fields: object) -> int:
        """部分更新——只寫呼叫端明確給的欄位。

        ``**fields`` 由 Service 過濾成「使用者真的有給的那幾個」；把 ``None`` 當成
        「設為空」寫進來的話，使用者改一次名稱、描述就不見了。
        """
        return self.get_queryset().filter(id=kb_id).update(**fields)


class DocumentRepository(SoftDeletableRepository[Document]):
    model = Document

    def get_by_id(self, document_id: uuid.UUID) -> Document | None:
        """租戶內以 id 找文件；不存在或屬於別的租戶都回 ``None``。

        回 None 而不是 raise 是刻意的：API 層要把它轉成 **404**（09 §2.3 的資源類
        規則）。回 403 等於承認「這個 id 存在，只是你不能碰」，那讓人可以拿 id 掃出
        別的租戶有哪些文件。
        """
        return self.get_queryset().filter(id=document_id).first()

    def for_kb(self, kb_id: uuid.UUID) -> list[Document]:
        """某個 KB 底下的文件——**不是**整個租戶的文件。

        漏了 kb 條件的話，回傳的每一筆都是呼叫者有權看的資料（同租戶），所以不會有
        錯誤也不會有紅燈；使用者只會覺得「這個知識庫怎麼有別的知識庫的文件」。
        """
        return list(self.get_queryset().filter(kb_id=kb_id).order_by("-created_at"))

    def count_for_kb(self, kb_id: uuid.UUID) -> int:
        return self.get_queryset().filter(kb_id=kb_id).count()

    def find_by_content_hash(self, *, kb_id: uuid.UUID, content_hash: str) -> Document | None:
        """上傳前的去重查詢。

        查詢範圍必須與 ``uq_document_tenant_kb_hash`` 逐字對應（租戶 + KB + hash）。
        查得比約束寬的話，上傳會回報「重複」但 INSERT 其實過得了；窄的話則是回報
        可以上傳、然後被 DB 擋下並冒出 IntegrityError。兩種不一致都很難從症狀反推。
        """
        return self.get_queryset().filter(kb_id=kb_id, content_hash=content_hash).first()

    def create(
        self,
        *,
        kb_id: uuid.UUID,
        filename: str,
        mime_type: str,
        storage_key: str,
        content_hash: str,
        size_bytes: int,
        source_type: str = "upload",
        document_id: uuid.UUID | None = None,
    ) -> Document:
        """建立文件列。

        ``document_id`` 由呼叫端指定是上傳流程的需求：物件 key 含 doc id，而物件必須
        在 DB 寫入**之前**就上傳完（見 DocumentService.upload 的順序說明）。不給時
        由 model 的 default 產生。
        """
        return Document.objects.create(
            id=document_id or uuid.uuid4(),
            tenant_id=get_current_tenant_id(operation="DocumentRepository.create"),
            kb_id=kb_id,
            filename=filename,
            mime_type=mime_type,
            storage_key=storage_key,
            content_hash=content_hash,
            size_bytes=size_bytes,
            source_type=source_type,
        )


class ChunkRepository(TenantScopedRepository[Chunk]):
    model = Chunk

    def for_document(self, document_id: uuid.UUID) -> list[Chunk]:
        """一份文件的全部 chunk，**按 ``seq`` 排序**。

        沒有明確 ORDER BY 時 PostgreSQL 不保證順序——小表通常剛好是插入順序，於是
        開發環境看起來正常，而重寫過的表（VACUUM、re-ingest）會突然變亂序。後果是
        文件預覽語意錯亂、相鄰 chunk 拼接時接錯段落。
        """
        return list(self.get_queryset().filter(document_id=document_id).order_by("seq"))

    def for_retrieval(self, *, kb_id: uuid.UUID) -> list[Chunk]:
        """檢索候選集：該 KB 底下**未 superseded** 的 chunk。

        條件與 ``ix_chunk_tenant_kb_active``（partial index）逐字對應，查詢才吃得到
        那個索引。
        """
        return list(self.get_queryset().filter(kb_id=kb_id, superseded=False).order_by("seq"))

    def mark_superseded(self, *, chunk_ids: Sequence[uuid.UUID]) -> int:
        """標記舊版本；回傳實際影響的列數。

        走 ``get_queryset()`` 出發（而非 ``Chunk.objects``）：這條 UPDATE 在 re-ingest
        時會以「整批」的形狀執行，漏了 tenant filter 就是把別的租戶的 chunk 一起標成
        superseded——受害租戶的檢索會突然回空集合，而沒有任何錯誤訊息。
        """
        return self.get_queryset().filter(id__in=list(chunk_ids)).update(superseded=True)


class EtlJobRepository(TenantScopedRepository[EtlJob]):
    model = EtlJob

    def find(self, *, doc_id: uuid.UUID, doc_version: int, stage: str) -> EtlJob | None:
        """依冪等鍵定位（08 §6）。三個欄位缺一不可，理由見本檔 docstring。"""
        return (
            self.get_queryset()
            .filter(document_id=doc_id, doc_version=doc_version, stage=stage)
            .first()
        )

    def create(self, *, doc_id: uuid.UUID, doc_version: int, stage: str) -> EtlJob:
        return EtlJob.objects.create(
            tenant_id=get_current_tenant_id(operation="EtlJobRepository.create"),
            document_id=doc_id,
            doc_version=doc_version,
            stage=stage,
        )

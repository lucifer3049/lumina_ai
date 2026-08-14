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

    def start_new_version(self, document_id: uuid.UUID, *, doc_version: int) -> int:
        """re-ingest：版本 +1 並回到起點（08 §2 的 ``ready → parsing`` 那條邊）。

        版本、狀態、error 一起寫：分開寫的話，中途失敗會留下「版本已經 +1 但狀態還是
        ready」的列——下一次重跑的冪等鍵指向新版本，於是舊 chunk 永遠不會被取代。
        """
        return (
            self.get_queryset()
            .filter(id=document_id)
            .update(doc_version=doc_version, status="uploaded", error=None)
        )

    def set_status(
        self, document_id: uuid.UUID, *, status: str, error: dict[str, object] | None = None
    ) -> int:
        """狀態機推進（08 §2）。``error`` 顯式傳 None 會清掉上一次的失敗紀錄。

        走 ``update`` 而不是讀出來改再存：ETL 與使用者的請求可能同時碰同一列，
        read-modify-write 會把對方的改動蓋掉（例如把已軟刪的文件寫回未刪）。
        """
        return self.get_queryset().filter(id=document_id).update(status=status, error=error)


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

    def supersede_for_document(self, document_id: uuid.UUID) -> int:
        """把一份文件目前所有未 superseded 的 chunk 標成舊版（re-ingest 用）。

        **標記而不是刪除**（05 §3.2）：新版本的 embedding 還沒好，這段期間檢索仍要
        服務得了查詢。刪掉的話，重跑進行中的那幾分鐘裡這份文件會完全查不到，而使用者
        的感受是「東西不見了」。舊列由清理 job 在重嵌入完成後硬刪。
        """
        return (
            self.get_queryset()
            .filter(document_id=document_id, superseded=False)
            .update(superseded=True)
        )

    def replace_for_version(
        self,
        *,
        document_id: uuid.UUID,
        kb_id: uuid.UUID,
        doc_version: int,
        rows: Sequence[dict[str, object]],
    ) -> int:
        """先刪同版本殘留再整批寫入（08 §6 的冪等）。

        **刪除是必要的，不是保險**：``uq_chunk_document_version_seq`` 會讓重跑在
        第一筆就撞唯一約束，而部分寫入的殘留（上次跑到一半崩潰）不刪就永遠卡住。
        兩件事在同一個交易裡，中途失敗時不會留下「刪了但沒寫」的空文件。

        ``bulk_create`` 而非逐筆 ``create``：一份 500 頁的 PDF 會有上千個 chunk，
        逐筆是上千次 round-trip。
        """
        self.get_queryset().filter(document_id=document_id, doc_version=doc_version).delete()
        tenant_id = get_current_tenant_id(operation="ChunkRepository.replace_for_version")
        created = Chunk.objects.bulk_create(
            [
                Chunk(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    kb_id=kb_id,
                    doc_version=doc_version,
                    **row,
                )
                for row in rows
            ]
        )
        return len(created)


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

    def start(self, *, doc_id: uuid.UUID, doc_version: int, stage: str) -> EtlJob:
        """取得或建立這個階段的 job，並標記為執行中、attempt +1。

        ``get_or_create`` 走 DB 的唯一約束（08 §6 的冪等鍵）而不是「先查再建」：
        併發觸發（使用者連點兩次、重試與排程同時進來）時，先查再建的兩邊都會查到
        「不存在」，於是各自建一筆——而那兩個 job 會同時處理同一份文件。
        """
        job, _ = EtlJob.objects.get_or_create(
            tenant_id=get_current_tenant_id(operation="EtlJobRepository.start"),
            document_id=doc_id,
            doc_version=doc_version,
            stage=stage,
        )
        job.status = "running"
        job.attempt += 1
        job.started_at = timezone.now()
        job.finished_at = None
        job.save(update_fields=["status", "attempt", "started_at", "finished_at", "updated_at"])
        return job

    def finish(
        self,
        job_id: uuid.UUID,
        *,
        status: str,
        stats: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
    ) -> int:
        """收尾（succeeded / failed）。``finished_at`` 一併寫，避免兩處各寫一半。"""
        return (
            self.get_queryset()
            .filter(id=job_id)
            .update(
                status=status,
                stats=stats or {},
                error=error,
                finished_at=timezone.now(),
            )
        )

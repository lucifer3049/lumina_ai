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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict

from django.db import connection, models
from django.utils import timezone
from pgvector import HalfVector
from pgvector.django import CosineDistance

from apps.knowledge.models import Chunk, Document, Embedding, EtlJob, KnowledgeBase
from core.tenant import get_current_tenant_id
from repositories.base import SoftDeletableRepository, TenantScopedRepository


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

    # ── 配額的存量聚合（04 §8.1，2A-2a）────────────────────────
    #
    # 文件數與儲存量的額度依據就是這兩個查詢——**不是 Redis 計數器**：存量必須活得
    # 比 Redis 久，且刪除要立即釋放。`get_queryset()` 已排除軟刪除（額度放的是使用
    # 者能控制的邏輯容量；物件儲存的實體位元組等清理 job）。

    def active_count(self) -> int:
        return self.get_queryset().count()

    def active_size_bytes(self) -> int:
        total = self.get_queryset().aggregate(total=models.Sum("size_bytes"))["total"]
        return int(total or 0)

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
        uploaded_by: uuid.UUID | None = None,
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
            uploaded_by=uploaded_by,
            source_type=source_type,
        )

    def stuck_in(self, statuses: Sequence[str], *, not_updated_since: datetime) -> list[Document]:
        """停在某幾個狀態、且一段時間沒有動靜的文件（維運恢復用）。

        ``not_updated_since`` 不是可有可無的參數：``uploaded`` 與 ``chunked`` 是**正常
        的過渡狀態**，剛上傳的文件本來就會在裡面待幾秒。少了時間下限，恢復指令會把
        正在被處理的文件再排一次——冪等保證那不會弄壞資料，但 embedding 那一段是真的
        錢，而重複的訊息會讓佇列在最需要吞吐的時候變成兩倍長。
        """
        return list(
            self.get_queryset()
            .filter(status__in=list(statuses), updated_at__lt=not_updated_since)
            .order_by("updated_at")
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

    def purge_superseded_of_ready(self) -> int:
        """硬刪 **ready 文件**的 superseded chunk 及其向量；回傳刪掉的 chunk 數（2A-2b）。

        只挑 ready：文件還在 embedding／failed 時，舊版是唯一完整的資料，重跑失敗時
        它是最後的退路。順序是先向量後 chunk——`Embedding.chunk` 是 PROTECT，反過來
        會被 DB 擋下（那個擋是對的：它保證這裡永遠不會只刪一半）。
        """
        chunks = self.get_queryset().filter(superseded=True, document__status="ready")
        Embedding.objects.filter(chunk__in=chunks).delete()
        deleted, _ = chunks.delete()
        return deleted

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

    def active_for_version(self, *, document_id: uuid.UUID, doc_version: int) -> list[Chunk]:
        """一份文件**目前這一版、未 superseded** 的 chunk——embedding 的輸入（1C-3）。

        兩個條件缺一不可。少了 ``doc_version``，re-ingest 之後會連舊版一起算；少了
        ``superseded``，同樣會算到即將被清理 job 硬刪的那些。兩種漏法的症狀都是帳單
        變貴而資料看起來正常。
        """
        return list(
            self.get_queryset()
            .filter(document_id=document_id, doc_version=doc_version, superseded=False)
            .order_by("seq")
        )

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


class EmbeddingRow(TypedDict):
    """一列待寫入的向量。

    ``model`` 與 ``embedding_version`` **不在這裡**：它們是整批共用的參數（一次
    Gateway 呼叫對應一個模型），放進每一列只會讓「同一批混了兩個模型」變成可能，
    而那是呼叫端組錯資料、不該由這一層容忍。
    """

    chunk_id: uuid.UUID
    vector: Sequence[float]


@dataclass(frozen=True, slots=True)
class VectorHit:
    """檢索命中的一筆——**這是 repository 唯一允許外流的形狀**。

    不回傳 `Embedding` 或 `Chunk` 物件：那會讓上層拿到一個綁著 ORM 的東西，而
    `rag/` 依鐵則 2 不得碰 ORM。``meta`` 保持原始 dict，page 與 heading_path 的解讀
    留給知道 1D 需要什麼的那一層。
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    # 檔名與版本在同一趟 SQL 裡撈回來（1D-5 的引用要用）：事後補查等於每則回答多 N
    # 次查詢，而引用面板是讀路徑上最頻繁的東西。join 的成本是零——`chunk__document`
    # 本來就在查詢裡。
    document_name: str
    doc_version: int
    content: str
    meta: dict[str, Any]
    score: float


class EmbeddingRepository(TenantScopedRepository[Embedding]):
    """向量的讀寫與檢索（05 §3.2、06 §3.1）。"""

    model = Embedding

    def upsert(
        self,
        rows: Sequence[EmbeddingRow],
        *,
        model: str,
        embedding_version: int,
    ) -> int:
        """寫入或覆蓋一批向量；回傳處理筆數。

        走唯一約束 + ``ON CONFLICT``（Django 的 ``update_conflicts``）而不是「先查再
        寫」：重嵌入是 at-least-once 的背景工作，而併發的兩個 worker 都會查到「不存
        在」，然後其中一個撞約束失敗——那一批的整份 API 呼叫（真的錢）就白做了。

        ``model`` 與 ``embedding_version`` 是整批共用的參數而不是每列一份：一次呼叫
        對應一個模型，混在同一批寫入代表呼叫端組錯了資料。
        """
        if not rows:
            return 0

        tenant_id = get_current_tenant_id(operation="EmbeddingRepository.upsert")
        Embedding.objects.bulk_create(
            [
                Embedding(
                    tenant_id=tenant_id,
                    chunk_id=row["chunk_id"],
                    model=model,
                    embedding_version=embedding_version,
                    vector=list(row["vector"]),
                )
                for row in rows
            ],
            update_conflicts=True,
            unique_fields=["chunk", "model", "embedding_version"],
            update_fields=["vector", "updated_at"],
        )
        return len(rows)

    def for_chunks(
        self, chunk_ids: Sequence[uuid.UUID], *, model: str, embedding_version: int
    ) -> list[Embedding]:
        return list(
            self.get_queryset().filter(
                chunk_id__in=list(chunk_ids),
                model=model,
                embedding_version=embedding_version,
            )
        )

    def search(
        self,
        query_vector: Sequence[float],
        *,
        kb_id: uuid.UUID,
        model: str,
        embedding_version: int,
        top_k: int,
        ef_search: int,
    ) -> list[VectorHit]:
        """向量檢索：回傳最相近的 chunk 與**相似度**（06 §3.1、05 §4）。

        三件事在這條查詢裡定案，錯了都不會報錯：

        1. **參數必須是 `HalfVector`**。欄位是 halfvec，而傳一般的 list 會被轉成
           `vector`；`halfvec <=> vector` 需要隱式轉型，於是 planner 用不到
           ``halfvec_cosine_ops`` 的索引——檢索退化成整表掃描，而結果完全正確。
           `tests/integration/test_vector_retrieval.py` 以 EXPLAIN 釘住這件事。

        2. **`ef_search` 用 ``SET LOCAL``**（11 §2 的 80）。它是「找多仔細」的旋鈕，
           預設值比文件定的低。用連線層級的 ``SET`` 會外溢到之後所有查詢——連線來自
           PgBouncer 的池，下一個拿到它的可能是完全不碰向量的 ETL。

        3. **回傳相似度而不是距離**（``1 - cosine_distance``）。兩者差一個負號，而都會
           排出一個看起來像答案的清單；06 §3.1 的 rerank 門檻 0.3 是相似度。

        過濾條件一個都不能少：租戶（``get_queryset``）、KB、``superseded``、以及
        model + embedding_version。少任何一個，回來的 chunk 都「確實存在且看起來合理」。
        """
        with connection.cursor() as cursor:
            # 參數化而非字面值：ef_search 目前是常數，但它遲早會變成 KB 可覆寫的設定
            # （06 §3.1 的「KB 可覆寫」），而那時這裡就是使用者輸入的落點。
            cursor.execute("SET LOCAL hnsw.ef_search = %s", [int(ef_search)])

        distance = CosineDistance("vector", HalfVector(list(query_vector)))
        rows = (
            self.get_queryset()
            .filter(
                chunk__kb_id=kb_id,
                chunk__superseded=False,
                model=model,
                embedding_version=embedding_version,
            )
            .annotate(distance=distance)
            .order_by("distance")
            .values_list(
                "chunk_id",
                "chunk__document_id",
                "chunk__document__filename",
                "chunk__doc_version",
                "chunk__content",
                "chunk__meta",
                "distance",
            )[:top_k]
        )
        return [
            VectorHit(
                chunk_id=chunk_id,
                document_id=document_id,
                document_name=filename or "",
                doc_version=int(doc_version),
                content=content,
                meta=meta or {},
                score=1.0 - float(distance_value),
            )
            for (
                chunk_id,
                document_id,
                filename,
                doc_version,
                content,
                meta,
                distance_value,
            ) in rows
        ]

    def chunks_without_embedding(
        self, chunk_ids: Sequence[uuid.UUID], *, model: str, embedding_version: int
    ) -> list[uuid.UUID]:
        """這批 chunk 裡還沒有向量的那些——1C-3 的批次依據。

        少了它，重跑會把整份文件重算一次，而每個 chunk 都是一次真的 API 呼叫。
        """
        existing = set(
            self.get_queryset()
            .filter(
                chunk_id__in=list(chunk_ids),
                model=model,
                embedding_version=embedding_version,
            )
            .values_list("chunk_id", flat=True)
        )
        return [chunk_id for chunk_id in chunk_ids if chunk_id not in existing]


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
